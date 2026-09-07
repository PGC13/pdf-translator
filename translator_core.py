"""
translator_core.py

Lógica central de extração e tradução de PDFs, compartilhada entre o CLI
(translate_pdf.py) e a interface web (web_app.py). Mantida num único lugar
para que as correções de bugs (cache de conexão do Ollama, reconstrução de
parágrafos, etc.) valham para os dois, sem duplicação.
"""

from __future__ import annotations

import subprocess
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
        return Path(out).resolve()
    tagged_name = f"{pdf_path.stem}.{model_size}.md"
    default_output_dir = Path("output")
    if default_output_dir.is_dir():
        return (default_output_dir / tagged_name).resolve()
    return pdf_path.with_name(tagged_name).resolve()


# --------------------------------------------------------------------------
# Conversão opcional para PDF (via pandoc + wkhtmltopdf)
# --------------------------------------------------------------------------

class PdfConversionError(RuntimeError):
    """Levantado quando a conversão para PDF falha -- por exemplo, se o
    pandoc ou o wkhtmltopdf não estiverem instalados. A tradução em si (o
    .md) já foi salva com sucesso antes desta etapa rodar; este erro nunca
    deve ser tratado como falha da tradução, só da conversão opcional."""


def convert_markdown_to_pdf(markdown_path: Path, pdf_path: Path, title: str) -> None:
    """Converte um arquivo Markdown já traduzido para PDF, usando pandoc com
    o motor wkhtmltopdf. Ambos são dependências externas OPCIONAIS -- só
    necessárias se o usuário pedir explicitamente a saída em PDF (--to-pdf no
    CLI, ou a opção correspondente na interface web); a tradução normal (.md)
    não depende delas."""
    try:
        subprocess.run(
            [
                "pandoc",
                str(markdown_path),
                "-o",
                str(pdf_path),
                "--pdf-engine=wkhtmltopdf",
                "--metadata",
                f"title={title}",
                "-V",
                "margin-top=20mm",
                "-V",
                "margin-bottom=20mm",
                "-V",
                "margin-left=20mm",
                "-V",
                "margin-right=20mm",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise PdfConversionError(
            "pandoc não encontrado no PATH. Instale em https://pandoc.org/installing.html "
            "e o wkhtmltopdf em https://wkhtmltopdf.org/downloads.html para poder gerar PDF "
            "(a tradução em .md já foi salva normalmente -- isso afeta só a conversão extra)."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise PdfConversionError(f"Falha ao converter para PDF: {exc.stderr.strip()}") from exc


# --------------------------------------------------------------------------
# Conversão opcional para HTML (biblioteca Python pura, sem dependência externa)
# --------------------------------------------------------------------------

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font-family: Georgia, serif; max-width: 780px; margin: 40px auto;
         padding: 0 20px; line-height: 1.6; color: #222; }}
  h1, h2, h3 {{ font-family: Arial, sans-serif; }}
  blockquote {{ border-left: 3px solid #ccc; margin-left: 0; padding-left: 1em; color: #555; }}
  code {{ background: #f4f4f4; padding: 2px 4px; border-radius: 3px; }}
  pre code {{ display: block; padding: 1em; overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


class HtmlConversionError(RuntimeError):
    """Levantado quando a conversão para HTML falha -- por exemplo, se a
    biblioteca `markdown` não estiver instalada. A tradução em si (o .md) já
    foi salva com sucesso antes desta etapa rodar."""


def convert_markdown_to_html(markdown_text: str, title: str) -> str:
    """Converte o Markdown já traduzido para um HTML autocontido (CSS embutido,
    um único arquivo). Ao contrário da conversão para PDF, esta não depende de
    nenhum programa externo -- só da biblioteca Python `markdown` (pip
    install markdown), então funciona sempre que o projeto estiver instalado,
    sem passo extra de configuração."""
    try:
        import markdown as _markdown  # import local -- só carregado se a função for chamada
    except ImportError as exc:
        raise HtmlConversionError(
            "biblioteca 'markdown' não encontrada. Instale com: pip install markdown"
        ) from exc

    body_html = _markdown.markdown(markdown_text, extensions=["tables", "fenced_code", "sane_lists"])
    return _HTML_TEMPLATE.format(title=title, body=body_html)
