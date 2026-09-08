"""
diagnostico_extracao.py

Ferramenta de diagnóstico -- não faz parte do fluxo normal do projeto, mas
fica guardada aqui para o caso de outro PDF disparar um problema parecido
com o que motivou a checagem de sanidade em extract_text_with_headings()
(ver README, seção "Detectar títulos/subtítulos").

Compara a contagem de palavras entre a extração simples (extract_text) e a
extração com detecção de cabeçalhos (extract_text_with_headings), sem
precisar rodar nenhuma tradução -- é a forma mais rápida de confirmar se um
PDF específico está sofrendo do problema de "camada de texto duplicada"
(alguns PDFs gerados via certas pipelines de LaTeX têm isso).

Uso:
    python tools/diagnostico_extracao.py caminho/do/artigo.pdf

Rode a partir da raiz do projeto (onde está o translator_core.py).
"""
import sys
from pathlib import Path

# Permite rodar este script estando na raiz do projeto, mesmo com o arquivo
# fisicamente dentro de tools/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from translator_core import extract_text, extract_text_with_headings, _HEADINGS_SANITY_RATIO

if len(sys.argv) != 2:
    print("Uso: python tools/diagnostico_extracao.py caminho/do/artigo.pdf")
    sys.exit(1)

pdf_path = Path(sys.argv[1])

print(f"Analisando: {pdf_path.name}")
print()

texto_simples = extract_text(pdf_path, page_range=None)
palavras_simples = len(texto_simples.split())
print(f"extract_text()               -> {palavras_simples} palavras")

texto_headings = extract_text_with_headings(pdf_path, page_range=None)
palavras_headings = len(texto_headings.split())
print(f"extract_text_with_headings() -> {palavras_headings} palavras")

print()
razao = palavras_headings / palavras_simples if palavras_simples else 0
print(f"Razão: {razao:.2f}x (limiar de recuo automático: {_HEADINGS_SANITY_RATIO}x)")
if razao > _HEADINGS_SANITY_RATIO:
    print(
        "⚠️  Acima do limiar -- extract_text_with_headings() já deveria ter "
        "recuado sozinha para a extração simples (ver aviso em web_app.log "
        "ou no terminal, se rodou via CLI)."
    )
else:
    print("✅ Dentro do esperado, sem indício de duplicação de conteúdo.")
