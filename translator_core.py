"""
translator_core.py

Lógica central de extração e tradução de PDFs, compartilhada entre o CLI
(translate_pdf.py) e a interface web (web_app.py). Mantida num único lugar
para que as correções de bugs (cache de conexão do Ollama, reconstrução de
parágrafos, etc.) valham para os dois, sem duplicação.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections import Counter
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


# --------------------------------------------------------------------------
# Detecção opcional de títulos/cabeçalhos (por tamanho de fonte)
# --------------------------------------------------------------------------
#
# A extração padrão acima (extract_text) junta tudo em texto corrido, sem
# distinguir títulos de parágrafos comuns -- porque o pypdf.extract_text()
# simples não preserva nenhuma informação visual, só texto puro. As funções
# abaixo usam uma API mais profunda do pypdf (visitor_text) que revela o
# TAMANHO REAL DA FONTE de cada trecho de texto, permitindo reconstruir
# títulos e subtítulos como cabeçalhos Markdown de verdade (#, ##, ###).
#
# Isso é opcional (flag --detect-headings / checkbox na interface web), não
# o padrão, por prudência: é uma mudança mais profunda na extração, e
# heurísticas de estrutura de PDF já nos surpreenderam antes (ver a nota
# técnica sobre detecção por comprimento de linha, que parecia funcionar em
# teste sintético e falhou em PDF real). Os limiares aqui usam RANKING
# relativo dos tamanhos de fonte (não faixas fixas), o que se mostrou mais
# robusto em testes -- mas ainda pode errar em PDFs com formatação incomum.


def _collect_page_fragments(page) -> list[tuple[float, str, float, bool]]:
    """Usa a API visitor_text do pypdf para capturar cada trecho de texto da
    página junto com sua posição vertical, o tamanho real da fonte, e se está
    em negrito (via o nome da fonte, ex: "Helvetica-Bold") -- dados que não
    vêm na chamada simples extract_text()."""
    fragments: list[tuple[float, str, float, bool]] = []

    def visitor(text, cm, tm, font_dict, font_size) -> None:
        if text.strip():
            base_font = (font_dict or {}).get("/BaseFont", "") if font_dict else ""
            is_bold = "bold" in base_font.lower()
            fragments.append((tm[5], text, font_size, is_bold))

    page.extract_text(visitor_text=visitor)
    return fragments


def _group_fragments_into_lines(
    fragments: list[tuple[float, str, float, bool]], y_tolerance: float = 2.0
) -> list[dict]:
    """Agrupa fragmentos que compartilham (aproximadamente) a mesma posição
    vertical numa única linha visual -- um PDF real frequentemente separa uma
    linha em vários fragmentos (ex: mudança de fonte no meio da linha).
    Também acumula a proporção de caracteres em negrito na linha, usada para
    detectar cabeçalhos que usam negrito em vez de (ou além de) fonte maior."""
    lines: list[dict] = []
    for y, text, size, bold in fragments:
        placed = False
        for line in lines:
            if abs(line["y"] - y) <= y_tolerance:
                line["text"] = (line["text"] + " " + text).strip()
                line["max_size"] = max(line["max_size"], size)
                line["bold_chars"] += len(text) if bold else 0
                line["total_chars"] += len(text)
                placed = True
                break
        if not placed:
            lines.append(
                {
                    "y": y,
                    "text": text.strip(),
                    "max_size": size,
                    "bold_chars": len(text) if bold else 0,
                    "total_chars": len(text),
                }
            )
    for line in lines:
        line["bold_ratio"] = line["bold_chars"] / max(1, line["total_chars"])
    return lines


# Proporção mínima de caracteres em negrito, na linha inteira, para considerar
# a linha candidata a cabeçalho por negrito (evita que UMA palavra em negrito
# no meio de uma frase normal seja confundida com um cabeçalho).
_BOLD_HEADING_RATIO = 0.8
# Comprimento máximo (caracteres) para uma linha em negrito ser considerada
# cabeçalho -- evita que um PARÁGRAFO inteiro em negrito (ex: um aviso/nota)
# seja tratado como título.
_BOLD_HEADING_MAX_LEN = 200


def _is_bold_heading_candidate(line: dict) -> bool:
    return line["bold_ratio"] >= _BOLD_HEADING_RATIO and len(line["text"]) <= _BOLD_HEADING_MAX_LEN


# Terceiro sinal: seções numeradas sem nenhuma distinção tipográfica (nem
# tamanho, nem negrito) -- comum em alguns PDFs onde o número da seção é a
# única pista visual (ex: "1. Introdução", "2.1 Coleta de Dados"). Detecta
# pelo PADRÃO do texto, não por metadado do PDF -- por isso é o sinal mais
# arriscado dos três: uma lista numerada de verdade ("1. Item\n2. Item\n3.
# Item") teria o mesmo formato de texto. A proteção contra isso é: só conta
# como cabeçalho se a linha vizinha (antes E depois) NÃO bater no mesmo
# padrão -- cabeçalhos de seção aparecem isolados, cercados de texto de
# corpo; itens de lista aparecem em sequência, um atrás do outro.
_NUMBERED_HEADING_RE = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,2})\.?\s+([A-ZÀ-Ý][^\n]{1,60})$")

# Limiar de sanidade: se a extração com detecção de cabeçalhos resultar em
# mais que essa proporção de palavras em relação à extração simples, é sinal
# de provável duplicação de conteúdo (encontrado em teste real com um PDF que
# tinha uma segunda camada de texto sobreposta) -- ver a checagem no final de
# extract_text_with_headings().
_HEADINGS_SANITY_RATIO = 1.3


def extract_text_with_headings(
    pdf_path: Path,
    page_range: Optional[tuple[int, int]] = None,
    min_heading_ratio: float = 1.15,
    max_heading_levels: int = 3,
) -> str:
    """Como extract_text(), mas tenta reconstruir títulos/subtítulos como
    cabeçalhos Markdown reais, detectados pelo tamanho da fonte (não pelo
    comprimento da linha, que já vimos ser pouco confiável).

    O nível de cada cabeçalho é definido pelo RANKING dos tamanhos de fonte
    distintos encontrados no documento inteiro (não por página, para evitar
    viés da primeira página, que costuma ter menos corpo de texto real
    proporcionalmente a título/autores) -- o maior tamanho vira nível 1, o
    segundo maior vira nível 2, etc. Só é considerado cabeçalho um tamanho
    pelo menos `min_heading_ratio` maior que o tamanho predominante do corpo
    do texto, para evitar falsos positivos (ex: uma linha de autores só um
    pouco maior que o corpo não deveria virar "cabeçalho").

    Os níveis de cabeçalho são deslocados (+2) no Markdown final para não
    colidir com o "## Página N" que já demarca cada página.
    """
    reader = PdfReader(str(pdf_path))
    total_pages = len(reader.pages)

    start, end = (1, total_pages) if page_range is None else page_range
    start = max(1, start)
    end = min(total_pages, end)

    if start > end:
        raise ValueError(f"Intervalo de páginas inválido: {start}-{end} (PDF tem {total_pages} páginas)")

    # Primeira passada: coleta as linhas de TODAS as páginas do intervalo,
    # necessário para calcular o tamanho do corpo do documento inteiro antes
    # de classificar qualquer linha individualmente.
    pages_lines: list[list[dict]] = []
    all_lines: list[dict] = []
    for i in range(start - 1, end):
        fragments = _collect_page_fragments(reader.pages[i])
        lines = _group_fragments_into_lines(fragments)
        pages_lines.append(lines)
        all_lines.extend(lines)

    if not any(line["text"] for lines in pages_lines for line in lines):
        raise ValueError(
            "Nenhum texto extraído. O PDF pode ser escaneado (imagem) e precisar de OCR "
            "(ex: Tesseract) antes da tradução."
        )

    # Tamanho do corpo do texto: o mais comum, ponderado por quantidade de
    # caracteres (corpo de texto real sempre domina em volume sobre
    # títulos/cabeçalhos, que são curtos).
    size_weight: Counter = Counter()
    for line in all_lines:
        if line["text"]:
            size_weight[round(line["max_size"])] += len(line["text"])
    body_size = size_weight.most_common(1)[0][0]

    candidate_sizes = sorted(
        {round(line["max_size"]) for line in all_lines if round(line["max_size"]) / body_size >= min_heading_ratio},
        reverse=True,
    )
    size_to_level = {size: min(i + 1, max_heading_levels) for i, size in enumerate(candidate_sizes)}
    # Cabeçalhos detectados só por negrito (mesmo tamanho do corpo -- padrão
    # comum em muitos artigos acadêmicos) ficam no próximo nível disponível
    # depois de todos os detectados por tamanho, já que não há como saber sua
    # posição hierárquica só pelo negrito.
    bold_only_level = min(len(candidate_sizes) + 1, max_heading_levels)

    # Segunda passada: monta o Markdown final, com cabeçalhos reais
    # intercalados com parágrafos de corpo reconstruídos (reaproveitando a
    # mesma lógica de rejoin_wrapped_lines para o texto entre cabeçalhos).
    chunks = []
    for page_num, lines in zip(range(start, end + 1), pages_lines):
        lines = [line for line in lines if line["text"]]
        if not lines:
            continue

        # Classifica cada linha pelos sinais mais confiáveis primeiro
        # (tamanho, depois negrito).
        line_levels = []
        for line in lines:
            level = size_to_level.get(round(line["max_size"]), 0)
            if level == 0 and _is_bold_heading_candidate(line):
                level = bold_only_level
            line_levels.append(level)

        # Terceiro sinal (padrão de seção numerada) só para linhas ainda sem
        # classificação -- e só conta se a linha vizinha (antes E depois)
        # não bater no mesmo padrão (ver nota acima sobre listas numeradas).
        pattern_matches = [
            _NUMBERED_HEADING_RE.match(line["text"]) if level == 0 else None
            for line, level in zip(lines, line_levels)
        ]
        for i, match in enumerate(pattern_matches):
            if match is None:
                continue
            prev_matches = pattern_matches[i - 1] is not None if i > 0 else False
            next_matches = pattern_matches[i + 1] is not None if i < len(pattern_matches) - 1 else False
            if prev_matches or next_matches:
                continue
            depth = match.group(1).count(".") + 1
            line_levels[i] = min(bold_only_level + (depth - 1), max_heading_levels)

        blocks: list[tuple] = []
        body_run: list[str] = []
        for line, level in zip(lines, line_levels):
            if level > 0:
                if body_run:
                    blocks.append(("body", "\n".join(body_run)))
                    body_run = []
                blocks.append(("heading", level, line["text"]))
            else:
                body_run.append(line["text"])
        if body_run:
            blocks.append(("body", "\n".join(body_run)))

        page_parts = [f"## Página {page_num}"]
        for block in blocks:
            if block[0] == "heading":
                _, level, text = block
                md_level = "#" * (level + 2)
                page_parts.append(f"{md_level} {text}")
            else:
                _, text = block
                page_parts.append(rejoin_wrapped_lines(text))
        chunks.append("\n\n".join(page_parts))

    result_text = "\n\n".join(chunks)

    # Checagem de sanidade: alguns PDFs (encontrado num artigo real, aparentemente
    # por causa de uma segunda "camada" de texto sobreposta no mesmo PDF, comum
    # em PDFs gerados via certas pipelines de LaTeX) fazem o visitor_text capturar
    # MUITO mais texto que a extração simples -- na prática, o mesmo conteúdo
    # duplicado, às vezes com caracteres corrompidos numa das cópias (ex: um
    # e-mail virando "goog/l.Vare.com" em vez de "google.com"). Isso não é uma
    # duplicação previsível o suficiente para "consertar" de forma confiável
    # (tentamos e desistimos -- ver histórico do projeto), então a proteção é
    # recuar automaticamente para a extração simples (comprovadamente estável)
    # sempre que o resultado parecer suspeito demais, em vez de arriscar
    # entregar um documento com conteúdo duplicado silenciosamente.
    plain_word_count = len(extract_text(pdf_path, page_range).split())
    headings_word_count = len(result_text.split())
    if plain_word_count > 0 and headings_word_count / plain_word_count > _HEADINGS_SANITY_RATIO:
        import warnings

        warnings.warn(
            f"Detecção de cabeçalhos produziu {headings_word_count} palavras contra "
            f"{plain_word_count} da extração simples (proporção "
            f"{headings_word_count / plain_word_count:.2f}x) -- sinal de possível "
            "duplicação de conteúdo (visto em PDFs com camadas de texto sobrepostas). "
            "Recuando automaticamente para a extração simples, sem detecção de "
            "cabeçalhos, para este documento.",
            stacklevel=2,
        )
        return extract_text(pdf_path, page_range)

    return result_text


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


class TranslationCancelledError(RuntimeError):
    """Levantado quando o cancelamento é solicitado (via cancel_check) entre
    duas chamadas ao Ollama. O cancelamento é cooperativo, não instantâneo:
    se uma chamada já está em andamento, ela termina normalmente antes do
    cancelamento surtir efeito -- não há forma segura de interromper uma
    chamada HTTP já em voo sem risco de deixar conexões/estado pela metade."""


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
    detect_headings: bool = False,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> TranslationResult:
    """Extrai e traduz um PDF, chamando progress_callback (se fornecido) a cada
    chamada concluída ao Ollama. Não escreve nenhum arquivo -- isso fica a
    cargo de quem chamar (CLI ou interface web), já que cada um decide o
    destino de forma diferente.

    'detect_headings' ativa a extração experimental que reconstrói títulos e
    subtítulos como cabeçalhos Markdown reais (por tamanho de fonte), em vez
    de tratar tudo como texto corrido. Desativado por padrão -- veja a
    documentação de extract_text_with_headings() para as limitações
    conhecidas dessa detecção.

    'cancel_check', se fornecido, é chamado antes de CADA chamada ao Ollama;
    se retornar True, a tradução é interrompida (levanta
    TranslationCancelledError) antes de iniciar a próxima chamada. O
    cancelamento é cooperativo -- uma chamada já em andamento sempre termina
    normalmente antes do cancelamento surtir efeito."""
    if detect_headings:
        source_md = extract_text_with_headings(pdf_path, page_range)
    else:
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
        if cancel_check is not None and cancel_check():
            raise TranslationCancelledError("Tradução cancelada pelo usuário.")
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
        # Restaurar os patches é sempre necessário, mesmo se cancelado ou se
        # der erro -- senão a correção "vaza" para chamadas futuras que usem
        # a mesma classe compartilhada da biblioteca.
        _polyglot_ollama.OllamaClient.generate = original_generate
        _polyglot_ollama.OllamaClient._get_client = original_get_client

    # O callback de "100% concluído" só é emitido aqui, DEPOIS do bloco
    # try/finally -- se translate_markdown levantar uma exceção (cancelamento
    # ou erro), essas linhas não são executadas, evitando emitir um falso
    # sinal de conclusão.
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
