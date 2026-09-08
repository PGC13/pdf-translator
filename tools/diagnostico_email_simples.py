"""Verifica especificamente se a extração SIMPLES (extract_text, sem
visitor_text nenhum) também produz e-mails corrompidos -- teste direto,
sem depender de contagem de palavras."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from translator_core import extract_text

if len(sys.argv) != 2:
    print("Uso: python tools/diagnostico_email_simples.py caminho/do/artigo.pdf")
    sys.exit(1)

pdf_path = Path(sys.argv[1])
texto = extract_text(pdf_path, page_range=None)

import re
emails = re.findall(r"[a-zA-Z0-9._-]+@[a-zA-Z0-9./]*\.[a-zA-Z]{2,}", texto)
print(f"Palavras totais: {len(texto.split())}")
print()
print("E-mails encontrados na extração SIMPLES (extract_text):")
for email in sorted(set(emails)):
    suspeito = "/" in email
    print(f"  {'⚠️ ' if suspeito else '✅ '}{email}")
