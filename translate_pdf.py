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

Também disponível como interface web (página única, tradução em background):
    streamlit run web_app.py
"""

import argparse
import asyncio
import sys
from pathlib import Path

from translator_core import (
    HtmlConversionError,
    PdfConversionError,
    Progress,
    build_output_markdown,
    convert_markdown_to_html,
    convert_markdown_to_pdf,
    default_output_path,
    parse_page_range,
    translate_document,
)


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


class _CliProgressPrinter:
    """Recebe callbacks de Progress e imprime uma barra de progresso no
    terminal, a cada 5% (ou a cada 120s de uma única chamada muito lenta),
    igual ao comportamento original do CLI."""

    def __init__(self) -> None:
        self.last_printed_pct_bucket = -1
        self.last_printed_time = 0.0
        self.is_first = True

    def __call__(self, progress: Progress) -> None:
        pct_bucket = (progress.pct // 5) * 5
        should_print = (
            pct_bucket != self.last_printed_pct_bucket
            or (progress.elapsed_s - self.last_printed_time) >= 120
        )
        display_pct = progress.pct if (self.is_first or progress.overflowed) else pct_bucket
        if not (should_print or progress.overflowed):
            return

        if progress.overflowed:
            total_display = f"{progress.done}/~{progress.estimated_total}+"
            eta_display = "?"
        else:
            total_display = f"{progress.done}/{progress.estimated_total}"
            eta_display = f"{progress.eta_s:.0f}s" if progress.eta_s is not None else "?"

        bar_width = 20
        filled = int(bar_width * display_pct / 100)
        bar = "█" * filled + "░" * (bar_width - filled)
        print(f"  [{bar}] {display_pct:3d}%  {total_display} chamadas  decorrido={progress.elapsed_s:.0f}s  eta={eta_display}")
        self.last_printed_pct_bucket = pct_bucket
        self.last_printed_time = progress.elapsed_s
        self.is_first = False


async def run(args: argparse.Namespace) -> None:
    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        print(f"Arquivo não encontrado: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    page_range = parse_page_range(args.pages)

    print(f"Extraindo texto de: {pdf_path.name}")
    print(f"Traduzindo ({args.source} -> {args.target}) usando modelo 'translategemma:{args.model_size}'...")
    if args.timeout is not None:
        print(f"Timeout por chamada ao Ollama ajustado para {args.timeout}s")

    printer = _CliProgressPrinter()
    result = await translate_document(
        pdf_path=pdf_path,
        source=args.source,
        target=args.target,
        model_size=args.model_size,
        page_range=page_range,
        chunk_size=args.chunk_size,
        timeout=args.timeout,
        progress_callback=printer,
    )

    print(f"Texto extraído: ~{result.word_count} palavras")
    print(f"Tempo decorrido até aqui: {result.elapsed_s:.1f}s")
    print(f"Tradução concluída ({result.ollama_calls} chamada(s) ao Ollama)")

    out_path = default_output_path(pdf_path, args.model_size, args.out)
    final_markdown = build_output_markdown(
        pdf_path.stem, args.source, args.target, f"translategemma:{args.model_size}", result.markdown
    )
    out_path.write_text(final_markdown, encoding="utf-8")

    print(f"Concluído. Tradução salva em: {out_path}")

    if args.to_pdf:
        pdf_out_path = out_path.with_suffix(".pdf")
        print("Convertendo para PDF (pandoc + wkhtmltopdf)...")
        try:
            convert_markdown_to_pdf(out_path, pdf_out_path, title=pdf_path.stem)
            print(f"PDF salvo em: {pdf_out_path}")
        except PdfConversionError as exc:
            print(f"Aviso: não foi possível gerar o PDF ({exc})", file=sys.stderr)

    if args.to_html:
        html_out_path = out_path.with_suffix(".html")
        print("Convertendo para HTML...")
        try:
            html_content = convert_markdown_to_html(final_markdown, title=pdf_path.stem)
            html_out_path.write_text(html_content, encoding="utf-8")
            print(f"HTML salvo em: {html_out_path}")
        except HtmlConversionError as exc:
            print(f"Aviso: não foi possível gerar o HTML ({exc})", file=sys.stderr)


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
    parser.add_argument(
        "--to-pdf",
        action="store_true",
        help=(
            "Além do .md, também gera um .pdf (mesmo nome, na mesma pasta). "
            "Requer pandoc (https://pandoc.org) e wkhtmltopdf "
            "(https://wkhtmltopdf.org) instalados e no PATH."
        ),
    )
    parser.add_argument(
        "--to-html",
        action="store_true",
        help=(
            "Além do .md, também gera um .html autocontido (mesmo nome, na "
            "mesma pasta). Requer a biblioteca 'markdown' (pip install "
            "markdown) -- sem dependências externas além dessa."
        ),
    )

    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
