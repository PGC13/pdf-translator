#!/usr/bin/env python3
"""
translate_pdf.py

Extrai o texto de um PDF e traduz usando TranslateGemma via Ollama (100% local),
salvando o resultado como Markdown.

Requisitos:
    pip install -r requirements.txt

    Ollama instalado e rodando localmente (https://ollama.com), com o modelo:
        ollama pull translategemma          # tamanho 4b (padrão)
        ollama pull translategemma:12b      # opcional, para --model-size 12b
        ollama pull translategemma:27b      # opcional, para --model-size 27b

Uso:
    python translate_pdf.py input/entrada.pdf --source en --target pt
    # Salva automaticamente em output/entrada.<model-size>.md

    # Traduzir apenas um intervalo de páginas
    python translate_pdf.py input/entrada.pdf --source en --target pt --pages 1-10

    # Escolher o tamanho do modelo (4b | 12b | 27b) e um timeout maior
    # (necessário para 12b/27b em GPUs com pouca VRAM -- veja o README)
    python translate_pdf.py input/entrada.pdf --source en --target pt --model-size 12b --timeout 600
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

from pypdf import PdfReader
from pypolyglot import translate_markdown, TranslateMarkdownOptions
import pypolyglot.ollama as _polyglot_ollama
from pypolyglot.translate import chunk_text as _chunk_text, get_chunk_size as _get_chunk_size
import httpx as _httpx


def _enable_windows_ansi() -> None:
    """No Windows, alguns consoles não processam sequências ANSI (usadas para
    limpar a linha da barra de progresso) por padrão. Isso habilita o suporte
    explicitamente; em outros sistemas operacionais, não faz nada."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass  # Não crítico -- se falhar, a barra ainda funciona, só sem limpeza de linha


def _rejoin_wrapped_lines(page_text: str) -> str:
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
            # Palavra quebrada por hífen de fim de linha -- remove o hífen e junta
            current = current[:-1] + line
        else:
            current = current.rstrip() + " " + line

    if current:
        paragraphs.append(current)

    return "\n\n".join(paragraphs)


def extract_text(pdf_path: Path, page_range: tuple[int, int] | None) -> str:
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
            page_text = _rejoin_wrapped_lines(page_text)
            chunks.append(f"## Página {i + 1}\n\n{page_text}")

    if not chunks:
        raise ValueError(
            "Nenhum texto extraído. O PDF pode ser escaneado (imagem) e precisar de OCR "
            "(ex: Tesseract) antes da tradução."
        )

    return "\n\n".join(chunks)


def parse_page_range(value: str | None) -> tuple[int, int] | None:
    if value is None:
        return None
    if "-" not in value:
        page = int(value)
        return (page, page)
    start_str, end_str = value.split("-", 1)
    return (int(start_str), int(end_str))


async def run(args: argparse.Namespace) -> None:
    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        print(f"Arquivo não encontrado: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    page_range = parse_page_range(args.pages)

    print(f"Extraindo texto de: {pdf_path.name}")
    source_md = extract_text(pdf_path, page_range)
    word_count = len(source_md.split())
    print(f"Texto extraído: ~{word_count} palavras")

    model = f"translategemma:{args.model_size}"
    print(f"Traduzindo ({args.source} -> {args.target}) usando modelo '{model}'...")

    if args.timeout is not None:
        _polyglot_ollama.GENERATE_TIMEOUT_S = args.timeout
        print(f"Timeout por chamada ao Ollama ajustado para {args.timeout}s")

    # A biblioteca pypolyglot tem dois problemas que, juntos, causam falhas mesmo
    # com o timeout padrão (60s): (1) o timeout de CONEXÃO é fixo em 10s no código,
    # separado do timeout de resposta; e (2) ela reaproveita (cacheia) a mesma
    # conexão HTTP entre chamadas diferentes — incluindo a checagem rápida "o
    # modelo já existe?" feita ANTES da tradução (com timeout curto de 10s) — e a
    # chamada de tradução em si acaba herdando esse timeout de 10s, mesmo quando
    # deveria usar o valor completo (60s por padrão, ou o definido via --timeout).
    # Aplicamos esta correção sempre (não só quando --timeout é usado), pois o bug
    # ocorre mesmo com o timeout padrão da biblioteca.
    async def _patched_get_client(self, timeout: float = _polyglot_ollama.GENERATE_TIMEOUT_S):
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = _httpx.AsyncClient(
            base_url=self.base_url,
            timeout=_httpx.Timeout(timeout, connect=timeout),
        )
        return self._client

    _polyglot_ollama.OllamaClient._get_client = _patched_get_client

    # Estima o número total de chamadas ao Ollama, replicando a mesma lógica de
    # divisão em trechos ("chunking") que a biblioteca usa internamente. É uma
    # aproximação (a biblioteca também segmenta por estrutura do Markdown antes
    # de aplicar esse limite de tamanho), mas fica próxima o suficiente para uma
    # barra de progresso útil.
    effective_chunk_size = args.chunk_size or _get_chunk_size(model)
    estimated_calls = max(1, len(_chunk_text(source_md, effective_chunk_size)))

    progress_state = {
        "completed": 0,
        "start": time.monotonic(),
        "last_printed_pct": -1,
        "last_printed_time": time.monotonic(),
    }
    _original_generate = _polyglot_ollama.OllamaClient.generate

    async def _patched_generate(self, req):
        response = await _original_generate(self, req)
        progress_state["completed"] += 1
        done = progress_state["completed"]
        overflowed = done > estimated_calls
        if overflowed:
            # A estimativa de chunks foi ultrapassada (a biblioteca segmenta de um
            # jeito um pouco diferente do que replicamos ao estimar). Nesse caso,
            # travamos em 99% em vez de recalcular total=done a cada chamada extra
            # -- senão a tela mostraria "100%" repetidamente antes do fim de fato.
            pct = 99
            total_display = f"{done}/~{estimated_calls}+"
            eta_display = "?"
        else:
            total = estimated_calls
            pct = min(99, int(done / total * 100))
            total_display = f"{done}/{total}"
        now = time.monotonic()
        elapsed = now - progress_state["start"]
        if not overflowed:
            avg = elapsed / done
            eta = avg * max(0, total - done)
            eta_display = f"{eta:.0f}s"

        # Imprime uma nova linha a cada 5% de progresso (~20 linhas no total,
        # independente de quantas chamadas o documento tiver). O fallback de tempo
        # é só para não deixar o usuário sem nenhum retorno caso uma única chamada
        # demore muito (ex: chunk grande em modelo lento, rodando em CPU).
        pct_bucket = (pct // 5) * 5
        is_first_print = progress_state["last_printed_pct"] == -1
        should_print = (
            pct_bucket != progress_state["last_printed_pct"]
            or (now - progress_state["last_printed_time"]) >= 120
        )
        # Primeira linha mostra o percentual real (ex: 1%), confirmando que começou.
        # As linhas seguintes mostram sempre o valor redondo da faixa de 5%
        # (5, 10, 15...), não o percentual exato daquele instante.
        display_pct = pct if (is_first_print or overflowed) else pct_bucket
        if should_print or overflowed:
            bar_width = 20
            filled = int(bar_width * display_pct / 100)
            bar = "█" * filled + "░" * (bar_width - filled)
            print(f"  [{bar}] {display_pct:3d}%  {total_display} chamadas  decorrido={elapsed:.0f}s  eta={eta_display}")
            progress_state["last_printed_pct"] = pct_bucket
            progress_state["last_printed_time"] = now

        return response

    _polyglot_ollama.OllamaClient.generate = _patched_generate

    start_time = time.monotonic()
    try:
        result = await translate_markdown(
            source_md,
            args.source,
            args.target,
            TranslateMarkdownOptions(model=model, batch_char_limit=args.chunk_size),
        )
    finally:
        _polyglot_ollama.OllamaClient.generate = _original_generate
        total_calls = progress_state["completed"]
        bar_width = 20
        print(f"  [{'█' * bar_width}] 100%  {total_calls}/{total_calls} chamadas (concluído)")
        elapsed = time.monotonic() - start_time
        print(f"Tempo decorrido até aqui: {elapsed:.1f}s")

    print(f"Tradução concluída ({result.ollama_calls} chamada(s) ao Ollama)")

    if args.out:
        out_path = Path(args.out)
    else:
        # Inclui o tamanho do modelo no nome do arquivo (ex: "artigo.4b.md"),
        # permitindo manter traduções de diferentes modelos lado a lado sem
        # uma sobrescrever a outra.
        tagged_name = f"{pdf_path.stem}.{args.model_size}.md"
        default_output_dir = Path("output")
        if default_output_dir.is_dir():
            out_path = default_output_dir / tagged_name
        else:
            out_path = pdf_path.with_name(tagged_name)
    header = (
        f"# {pdf_path.stem}\n\n"
        f"> Traduzido automaticamente de **{args.source}** para **{args.target}** "
        f"com TranslateGemma ({model}), local via Ollama.\n\n---\n\n"
    )
    out_path.write_text(header + result.markdown, encoding="utf-8")

    print(f"Concluído. Tradução salva em: {out_path}")


def main() -> None:
    _enable_windows_ansi()
    parser = argparse.ArgumentParser(
        description="Extrai texto de um PDF e traduz localmente com TranslateGemma via Ollama."
    )
    parser.add_argument("pdf_path", help="Caminho do arquivo PDF de entrada")
    parser.add_argument("--source", default="en", help="Código do idioma de origem (padrão: en)")
    parser.add_argument("--target", default="pt", help="Código do idioma de destino (padrão: pt)")
    parser.add_argument(
        "--out",
        default=None,
        help="Caminho do arquivo .md de saída (padrão: output/<nome_do_pdf>.<model-size>.md)",
    )
    parser.add_argument("--pages", default=None, help="Intervalo de páginas, ex: 1-10 (padrão: todas)")
    parser.add_argument(
        "--model-size",
        default="4b",
        choices=["4b", "12b", "27b"],
        help="Tamanho do modelo TranslateGemma (padrão: 4b, mais rápido)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=None,
        help=(
            "Tamanho máximo (em caracteres) de cada trecho enviado ao modelo por vez. "
            "Reduza este valor se ocorrerem erros de timeout (ex: 1500 para modelos "
            "grandes/máquinas sem GPU potente). Padrão: definido automaticamente pela "
            "biblioteca conforme o tamanho do modelo (2000 para 4b, 4000 para 12b, "
            "6000 para 27b)."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            "Tempo máximo (em segundos) de espera por cada chamada ao Ollama. "
            "A biblioteca usa 60s por padrão, insuficiente para modelos que não "
            "cabem na VRAM da GPU e caem para CPU. Aumente este valor (ex: 300) "
            "se ocorrer erro OLLAMA_TIMEOUT mesmo com --chunk-size reduzido."
        ),
    )

    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
