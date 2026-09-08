"""
diagnostico_paginas.py

Ferramenta de diagnóstico -- ver tools/diagnostico_extracao.py para contexto
geral. Varre TODAS as páginas do PDF, contando fragmentos e palavras por
página, e procura conteúdo duplicado -- tanto dentro da mesma página quanto
entre páginas diferentes (ex: a mesma página aparecendo fisicamente duas
vezes no PDF). Útil para localizar EM QUAIS páginas uma duplicação de
conteúdo está concentrada (ou confirmar que está difusa por todo o
documento, como no caso real que motivou esta ferramenta).

Uso:
    python tools/diagnostico_paginas.py caminho/do/artigo.pdf
"""
import sys
from pathlib import Path
from collections import Counter

from pypdf import PdfReader

if len(sys.argv) != 2:
    print("Uso: python tools/diagnostico_paginas.py caminho/do/artigo.pdf")
    sys.exit(1)

pdf_path = Path(sys.argv[1])
reader = PdfReader(str(pdf_path))
total_pages = len(reader.pages)
print(f"Total de páginas: {total_pages}\n")

all_page_texts = []  # uma "assinatura" (primeiros 200 caracteres) por página

for i in range(total_pages):
    fragments = []

    def visitor(text, cm, tm, font_dict, font_size, _frags=fragments):
        if text.strip():
            _frags.append(text.strip())

    reader.pages[i].extract_text(visitor_text=visitor)

    full_text = " ".join(fragments)
    word_count = len(full_text.split())
    signature = full_text[:200]
    all_page_texts.append(signature)

    # Verifica duplicação DENTRO da mesma página (fragmentos de texto idênticos)
    counter = Counter(fragments)
    repeated_in_page = {t: n for t, n in counter.items() if n > 1 and len(t) > 15}

    flag = ""
    if repeated_in_page:
        flag = f"  ⚠️ {len(repeated_in_page)} fragmentos repetidos NESTA página"

    print(f"Página {i+1:2d}: {len(fragments):4d} fragmentos, ~{word_count:4d} palavras{flag}")

# Verifica duplicação ENTRE páginas (páginas diferentes com conteúdo inicial idêntico)
print()
print("Verificando páginas com conteúdo inicial idêntico entre si...")
sig_counter = Counter(all_page_texts)
found_cross_page_dup = False
for sig, n in sig_counter.items():
    if n > 1 and len(sig.strip()) > 20:
        found_cross_page_dup = True
        matching_pages = [i + 1 for i, s in enumerate(all_page_texts) if s == sig]
        print(f"  ⚠️ Páginas {matching_pages} começam com o MESMO texto: {sig[:80]!r}")
if not found_cross_page_dup:
    print("  Nenhuma página duplicada fisicamente encontrada.")

print()
print("Diagnóstico concluído.")
