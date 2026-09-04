"""
translator_core.py

Lógica central de extração e tradução de PDFs, compartilhada entre o CLI
(translate_pdf.py) e a interface web (web_app.py). Mantida num único lugar
para que as correções de bugs (cache de conexão do Ollama, reconstrução de
parágrafos, etc.) valham para os dois, sem duplicação.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from pypdf import PdfReader
from pypolyglot import translate_markdown, TranslateMarkdownOptions
import pypolyglot.ollama as _polyglot_ollama
from pypolyglot.translate import chunk_text as _chunk_text, get_chunk_size as _get_chunk_size
import httpx as _httpx


# --------------------------------------------------------------------------
# Extração e limpeza de texto
# --------------------------------------------------------------------------

def rejoin_wrapped_lines(page_text: str) -> str:
    """PDFs (especialmente artigos acadêmicos em duas colunas) quebram linhas na
    largura da coluna, não no fim de frases/parágrafos. Se não corrigirmos isso,
    o texto é traduzido "picado" (uma linha do PDF vira uma linha da tradução),
    resultando num Markdown com parágrafos fragmentados de forma artificial.

    Uma primeira versão desta função tentava detectar o fim de cada parágrafo
    (heurística de "linha mais curta que o padrão da página"), mas isso se mostrou
    pouco confiável em páginas reais, onde título, autores e corpo do texto têm
    larguras de linha bem diferentes -- causava fragmentação EXCESSIVA (um
    "parágrafo" por frase) e disparava um número de chamadas à API muito maior
    que o necessário. A abordagem atual é mais simples e previsível: junta TODAS
    as linhas consecutivas sem quebra em branco no meio numa única sequência de
    texto corrido, respeitando apenas as quebras em branco já presentes na
    extração (que costumam separar seções reais, como título/resumo/corpo).
    Também remove hifenização de quebra de linha (ex: "compre-\\nensão" -> "compreensão").
    """
    raw_lines = [ln.strip() for ln in page_text.splitlines()]
    paragraphs: list[str] = []
    current = ""

    for line in raw_lines:
        if not line:
            if current:
                paragraphs.append(current)
                current = ""
            continue
        if not current:
            current = line
        elif current.endswith("-") and not current.endswith("--"):
            current = current[:-1] + line
        else:
            current = current.rstrip() + " " + line

    if current:
        paragraphs.append(current)

    return "\n\n".join(paragraphs)


def extract_text(pdf_path: Path, page_range: Optional[tuple[int, int]]) -> str:
    """Extrai o texto de um PDF, opcionalmente limitado a um intervalo de páginas (1-indexed)."""
    reader = PdfReader(str(pdf_path))
    total_pages = len(reader.pages)

    start, end = (1, total_pages) if page_range is None else page_range
    start = max(1, start)
    end = min(total_pages, end)

    if start > end:
        raise ValueError(f"Intervalo de páginas inválido: {start}-{end} (PDF tem {total_pages} páginas)")

    chunks = []
    for i in range(start - 1, end):
        page_text = reader.pages[i].extract_text() or ""
        page_text = page_text.strip()
        if page_text:
            page_text = rejoin_wrapped_lines(page_text)
            chunks.append(f"## Página {i + 1}\n\n{page_text}")

    if not chunks:
        raise ValueError(
            "Nenhum texto extraído. O PDF pode ser escaneado (imagem) e precisar de OCR "
            "(ex: Tesseract) antes da tradução."
        )

    return "\n\n".join(chunks)


def parse_page_range(value: Optional[str]) -> Optional[tuple[int, int]]:
    if value is None:
        return None
    if "-" not in value:
        page = int(value)
        return (page, page)
    start_str, end_str = value.split("-", 1)
    return (int(start_str), int(end_str))


# --------------------------------------------------------------------------
# Progresso
# --------------------------------------------------------------------------

@dataclass
class Progress:
    """Snapshot do progresso de uma tradução em andamento, passado a cada
    atualização para o callback fornecido por quem chamou translate_document()."""
    done: int
    estimated_total: int
    overflowed: bool  # True quando 'done' já ultrapassou a estimativa inicial
    pct: int  # 0-100 (trava em 99 até o fim de fato, quando overflowed)
    elapsed_s: float
    eta_s: Optional[float]  # None quando overflowed (não dá pra estimar)


@dataclass
class TranslationResult:
    markdown: str
    ollama_calls: int
    elapsed_s: float
    word_count: int


ProgressCallback = Callable[[Progress], None]


# --------------------------------------------------------------------------
# Correção do bug de cache de conexão do Ollama (ver nota detalhada abaixo)
# --------------------------------------------------------------------------

def _apply_ollama_client_patch() -> Callable:
    """A biblioteca pypolyglot tem dois problemas que, juntos, causam falhas mesmo
    com o timeout padrão (60s): (1) o timeout de CONEXÃO é fixo em 10s no código,
    separado do timeout de resposta; e (2) ela reaproveita (cacheia) a mesma
    conexão HTTP entre chamadas diferentes -- incluindo a checagem rápida "o
    modelo já existe?" feita ANTES da tradução (com timeout curto de 10s) -- e a
    chamada de tradução em si acaba herdando esse timeout de 10s, mesmo quando
    deveria usar o valor completo (60s por padrão, ou o definido pelo usuário).
    Aplicamos esta correção sempre (não só quando um timeout customizado é
    usado), pois o bug ocorre mesmo com o timeout padrão da biblioteca.

    Retorna o método original, para quem chamar poder restaurá-lo depois
    (a lib usa uma classe compartilhada em nível de módulo, então a correção
    "vaza" para outras chamadas se não for desfeita ao final)."""
    original = _polyglot_ollama.OllamaClient._get_client

    async def _patched_get_client(self, timeout: float = _polyglot_ollama.GENERATE_TIMEOUT_S):
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = _httpx.AsyncClient(
            base_url=self.base_url,
            timeout=_httpx.Timeout(timeout, connect=timeout),
        )
        return self._client

    _polyglot_ollama.OllamaClient._get_client = _patched_get_client
    return original


# --------------------------------------------------------------------------
# Tradução
# --------------------------------------------------------------------------

async def translate_document(
    pdf_path: Path,
    source: str,
    target: str,
    model_size: str = "4b",
    page_range: Optional[tuple[int, int]] = None,
    chunk_size: Optional[int] = None,
    timeout: Optional[float] = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> TranslationResult:
    """Extrai e traduz um PDF, chamando progress_callback (se fornecido) a cada
    chamada concluída ao Ollama. Não escreve nenhum arquivo -- isso fica a
    cargo de quem chamar (CLI ou interface web), já que cada um decide o
    destino de forma diferente."""
    source_md = extract_text(pdf_path, page_range)
    word_count = len(source_md.split())

    model = f"translategemma:{model_size}"

    if timeout is not None:
        _polyglot_ollama.GENERATE_TIMEOUT_S = timeout

    original_get_client = _apply_ollama_client_patch()

    effective_chunk_size = chunk_size or _get_chunk_size(model)
    estimated_calls = max(1, len(_chunk_text(source_md, effective_chunk_size)))

    state = {"completed": 0, "start": time.monotonic()}
    original_generate = _polyglot_ollama.OllamaClient.generate

    async def _patched_generate(self, req):
        response = await original_generate(self, req)
        state["completed"] += 1
        done = state["completed"]
        overflowed = done > estimated_calls
        elapsed = time.monotonic() - state["start"]

        if overflowed:
            pct = 99
            eta = None
        else:
            pct = min(99, int(done / estimated_calls * 100))
            avg = elapsed / done
            eta = avg * max(0, estimated_calls - done)

        if progress_callback is not None:
            progress_callback(
                Progress(
                    done=done,
                    estimated_total=estimated_calls,
                    overflowed=overflowed,
                    pct=pct,
                    elapsed_s=elapsed,
                    eta_s=eta,
                )
            )
        return response

    _polyglot_ollama.OllamaClient.generate = _patched_generate

    start_time = time.monotonic()
    try:
        result = await translate_markdown(
            source_md,
            source,
            target,
            TranslateMarkdownOptions(model=model, batch_char_limit=chunk_size),
        )
    finally:
        _polyglot_ollama.OllamaClient.generate = original_generate
        _polyglot_ollama.OllamaClient._get_client = original_get_client
        elapsed = time.monotonic() - start_time
        if progress_callback is not None:
            progress_callback(
                Progress(
                    done=state["completed"],
                    estimated_total=state["completed"],
                    overflowed=False,
                    pct=100,
                    elapsed_s=elapsed,
                    eta_s=0,
                )
            )

    return TranslationResult(
        markdown=result.markdown,
        ollama_calls=result.ollama_calls,
        elapsed_s=elapsed,
        word_count=word_count,
    )


def build_output_markdown(pdf_stem: str, source: str, target: str, model: str, translated_markdown: str) -> str:
    """Monta o Markdown final com o cabeçalho padrão, usado tanto pelo CLI
    quanto pela interface web."""
    header = (
        f"# {pdf_stem}\n\n"
        f"> Traduzido automaticamente de **{source}** para **{target}** "
        f"com TranslateGemma ({model}), local via Ollama.\n\n---\n\n"
    )
    return header + translated_markdown


def default_output_path(pdf_path: Path, model_size: str, out: Optional[str] = None) -> Path:
    if out:
        return Path(out)
    tagged_name = f"{pdf_path.stem}.{model_size}.md"
    default_output_dir = Path("output")
    if default_output_dir.is_dir():
        return default_output_dir / tagged_name
    return pdf_path.with_name(tagged_name)
