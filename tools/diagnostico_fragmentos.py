"""
diagnostico_fragmentos.py

Ferramenta de diagnóstico -- ver tools/diagnostico_extracao.py para contexto
geral. Este script mostra os fragmentos BRUTOS capturados pelo visitor_text
do pypdf numa página específica (posição vertical, tamanho de fonte, texto),
útil para investigar visualmente onde/como uma duplicação de conteúdo está
acontecendo, uma vez que tools/diagnostico_extracao.py já confirmou que ela
existe.

Uso:
    python tools/diagnostico_fragmentos.py caminho/do/artigo.pdf [numero_da_pagina]

numero_da_pagina é 1-indexed (padrão: 1, a primeira página).
"""
import sys
from pathlib import Path
from collections import Counter

from pypdf import PdfReader

if len(sys.argv) not in (2, 3):
    print("Uso: python tools/diagnostico_fragmentos.py caminho/do/artigo.pdf [numero_da_pagina]")
    sys.exit(1)

pdf_path = Path(sys.argv[1])
page_num = int(sys.argv[2]) if len(sys.argv) == 3 else 1

reader = PdfReader(str(pdf_path))
if not (1 <= page_num <= len(reader.pages)):
    print(f"Página {page_num} inválida -- o PDF tem {len(reader.pages)} páginas.")
    sys.exit(1)

page = reader.pages[page_num - 1]

print(f"Total de páginas no PDF: {len(reader.pages)}")
print(f"Analisando página: {page_num}")
print()

fragments = []


def visitor(text, cm, tm, font_dict, font_size):
    if text.strip():
        fragments.append((tm[5], text.strip(), font_size))


page.extract_text(visitor_text=visitor)

print(f"Total de fragmentos capturados: {len(fragments)}")
print()
print("Primeiros 40 fragmentos (posição Y, tamanho fonte, texto):")
for y, text, size in fragments[:40]:
    print(f"  y={y:7.1f}  size={size:5.1f}  {text[:60]!r}")

# Verifica se existem fragmentos com o MESMO texto exato repetido
texts_only = [t for _, t, _ in fragments]
counter = Counter(texts_only)
repeated = {t: n for t, n in counter.items() if n > 1 and len(t) > 15}
if repeated:
    print()
    print(f"⚠️ {len(repeated)} textos distintos aparecem repetidos (mais de uma vez):")
    for text, n in list(repeated.items())[:10]:
        print(f"  ({n}x) {text[:70]!r}")
else:
    print()
    print("Nenhum fragmento com texto exatamente repetido nesta página.")
