# pdf-translator

**Português** | [English](README.en.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

Tradução de PDFs 100% local, sem depender de nenhuma API externa ou serviço de nuvem.
Extrai o texto de um PDF e traduz usando [TranslateGemma](https://ollama.com/library/translategemma)
rodando via [Ollama](https://ollama.com), salvando o resultado em Markdown.

## Capturas de tela

<p align="center">
  <img src="docs/screenshots/web-idle.png" alt="Interface web, pronta para uso" width="32%">
  <img src="docs/screenshots/web-translating.png" alt="Tradução em andamento" width="32%">
  <img src="docs/screenshots/web-completed.png" alt="Tradução concluída, pronta para baixar" width="32%">
</p>

## Por quê

Ferramentas de tradução em nuvem (Google Translate, DeepL, etc.) exigem enviar o
conteúdo do documento para servidores de terceiros — inconveniente para material
sensível, de estudo, ou institucional. Este projeto roda inteiramente na sua
máquina: nenhum dado sai do computador.

## Como funciona

1. **Extração** (`pypdf`) — lê o texto de cada página do PDF.
2. **Reconstrução de parágrafos** — junta linhas que são apenas quebra visual
   de coluna (comum em PDFs acadêmicos de duas colunas), corrigindo
   hifenização de quebra de linha, para que o texto flua como prosa normal
   antes de ser traduzido.
3. **Tradução** (`polyglot-gpu` + Ollama) — envia o texto em trechos para o
   TranslateGemma, rodando localmente via Ollama, preservando a estrutura
   Markdown.
4. **Saída** — salva o resultado como `.md`, com cabeçalho indicando idiomas e
   modelo usados.

Essa lógica vive em `translator_core.py` e é usada tanto pelo CLI
(`translate_pdf.py`) quanto pela [interface web](#interface-web)
(`web_app.py`) -- escolha a que preferir, o motor por trás é o mesmo.

## Requisitos

- Python 3.10+
- [Ollama](https://ollama.com/download) instalado e rodando
- Modelo TranslateGemma baixado (o comando abaixo baixa o tamanho `4b`, usado
  por padrão):
  ```bash
  ollama pull translategemma
  ```
  Para usar `--model-size 12b` ou `27b`, baixe o tamanho correspondente antes
  (senão o script falha com erro de "modelo não encontrado"):
  ```bash
  ollama pull translategemma:12b
  ollama pull translategemma:27b
  ```

## Instalação

```bash
git clone https://github.com/PGC13/pdf-translator.git
cd pdf-translator

# Opcional, mas recomendado: isolar as dependências num ambiente virtual
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### Estrutura de pastas

```
pdf-translator/
├── input/     # coloque aqui os PDFs que quer traduzir
├── output/    # as traduções em .md são salvas aqui
├── translator_core.py   # lógica compartilhada (extração + tradução)
├── translate_pdf.py     # interface de linha de comando
├── web_app.py            # interface web (Streamlit)
└── web_app.log            # log da interface web (gerado ao rodar, não versionado)
```

## Interface Web

Além da linha de comando, o projeto tem uma interface web em Streamlit --
página única, sem login (uso local), com barra de progresso ao vivo:

```bash
streamlit run web_app.py
```

Abre em `http://localhost:8501`. A tradução roda numa thread em segundo
plano: você pode trocar de aba ou deixar o navegador minimizado que ela
continua -- a página só consulta o progresso periodicamente. Ao terminar,
aparece um botão para baixar o `.md`, uma pré-visualização do resultado, e o
arquivo já foi salvo automaticamente em `output/<nome_do_pdf>.<model-size>.md`
(mesma convenção de nome do CLI).

**Importante:** isso vale para a **aba do navegador**, não para o terminal.
Se você fechar a janela do PowerShell onde rodou `streamlit run`, o processo
inteiro é encerrado -- incluindo a tradução em andamento, sem nenhum aviso
ou erro registrado (o Windows simplesmente mata tudo junto). Para rodar
outro comando enquanto uma tradução está em curso, abra uma **janela nova**
do PowerShell, sem fechar a que está rodando o Streamlit.

**Limitação atual:** não há como cancelar uma tradução em andamento pela
interface -- só é possível iniciar uma nova depois que a atual terminar
(com sucesso ou erro).

A lógica de extração e tradução (incluindo as correções de bugs descritas
mais abaixo) fica em `translator_core.py`, usada tanto pelo CLI quanto pela
interface web -- nenhuma duplicação, uma correção vale para os dois.

<details>
<summary><strong>Logs, bug de progresso corrigido, e por que não há executável (clique para expandir)</strong></summary>

### Logs

A interface web grava um log detalhado em `web_app.log` (criado na pasta do
projeto ao rodar), cobrindo tanto a página principal quanto a thread de
tradução em segundo plano -- útil para diagnosticar qualquer travamento ou
comportamento inesperado sem depender só do que aparece na tela. Para
acompanhar em tempo real:

```powershell
# Windows (PowerShell)
Get-Content web_app.log -Wait
```
```bash
# Linux/macOS
tail -f web_app.log
```

### Bug encontrado e corrigido: progresso não atualizava na tela

Numa versão inicial, o progresso da tradução era guardado num dicionário
Python comum, em memória, em nível de módulo. O job era criado corretamente
(confirmado via log), mas a leitura imediatamente após um `st.rerun()` não o
encontrava mais -- o dicionário não sobrevivia de forma confiável entre
execuções sucessivas do script nesse ambiente. Como consequência, a tela
ficava presa mostrando "nenhuma tradução em andamento" mesmo com a tradução
rodando (e terminando corretamente) em segundo plano.

A correção: o estado de cada tradução agora é persistido em **arquivos JSON
em disco** (na pasta temporária do sistema), não em memória. Isso elimina o
problema por completo, já que não depende de nenhuma variável Python
sobreviver entre execuções do script -- só de arquivos existirem no disco,
o que é sempre confiável. Esse mesmo mecanismo também é o que permite à
interface reconectar ao progresso correto mesmo depois de um F5 no meio de
uma tradução longa.

### Sobre atalhos de duplo clique / executáveis

Testamos criar atalhos `.bat`/`.vbs` para iniciar a interface web com duplo
clique, sem precisar digitar comando nenhum. Abandonamos essa ideia: no
Windows 11 com **Smart App Control** ativado (comum em máquinas com postura de
segurança mais rígida), esses arquivos são bloqueados por padrão -- e
diferente do Defender comum, o Smart App Control não tem uma exceção fácil de
conceder (desativá-lo exige reinstalar o Windows).

Por esse mesmo motivo, também não empacotamos um `.exe` "de verdade" via
PyInstaller: além de sofrer do mesmo tipo de bloqueio (ou pior, já que
executáveis empacotados são um alvo clássico de falso positivo de heurística
de antivírus, exigindo um certificado de assinatura de código pago para
evitar isso de forma confiável), o Ollama continuaria sendo uma dependência
externa de qualquer forma, então nunca seria realmente "standalone".

O comando direto abaixo é simples, transparente, e não esbarra em nenhuma
dessas restrições:

```bash
streamlit run web_app.py
```

</details>

## Uso (linha de comando)

```bash
python translate_pdf.py input/entrada.pdf --source en --target pt
```

<p align="center">
  <img src="docs/screenshots/cli-progress.png" alt="Barra de progresso no terminal, do início à conclusão" width="70%">
</p>

O resultado é salvo automaticamente em `output/entrada.<model-size>.md` (ex:
`entrada.4b.md`). Use `--out` para escolher outro caminho.

### Opções

| Flag | Descrição | Padrão |
|---|---|---|
| `--source` | Código do idioma de origem | `en` |
| `--target` | Código do idioma de destino | `pt` |
| `--out` | Caminho do `.md` de saída | `output/<nome_do_pdf>.<model-size>.md` (ex: `artigo.4b.md`) |
| `--pages` | Intervalo de páginas, ex. `1-10` | todas |
| `--model-size` | `4b`, `12b` ou `27b` | `4b` |
| `--chunk-size` | Tamanho máximo (caracteres) de cada trecho enviado ao modelo por vez | automático (2000/4000/6000 conforme o modelo) |
| `--timeout` | Tempo máximo (segundos) de espera por chamada ao Ollama | 60s (da biblioteca) -- veja a seção "Timeout" abaixo |
| `--to-pdf` | Também gera um `.pdf` (requer pandoc + wkhtmltopdf) | desativado |
| `--to-html` | Também gera um `.html` autocontido (só requer a lib `markdown`) | desativado |

### Exemplos

```bash
# Traduzir um paper inteiro de inglês para português (saída automática:
# output/paper.4b.md)
python translate_pdf.py input/paper.pdf --source en --target pt

# Traduzir só as 5 primeiras páginas, com o modelo maior (mais qualidade;
# requer 'ollama pull translategemma:12b' antes, e --timeout generoso)
python translate_pdf.py input/paper.pdf --source en --target pt --pages 1-5 --model-size 12b --timeout 600

# Salvar em local específico
python translate_pdf.py input/paper.pdf --source en --target es --out output/traduzido.md
```

## Limitações

- Funciona apenas com PDFs que já têm texto extraível (não escaneados/imagem).
  Para PDFs escaneados, é necessário OCR (ex: Tesseract) antes de rodar este script.
- O tempo de tradução varia muito conforme o tamanho do modelo e quanto dele
  cabe na VRAM da GPU — veja a seção "Qual tamanho de modelo escolher" abaixo
  para números reais medidos.

### PDFs em colunas (artigos acadêmicos)

PDFs de artigos acadêmicos costumam ter texto em duas colunas, e ferramentas de
extração (`pypdf`) capturam o texto respeitando as quebras de linha *visuais*
de cada coluna, não as quebras reais de frase ou parágrafo. Sem correção, isso
resultaria numa tradução "picada" — cada linha do PDF virando uma linha
separada no Markdown, e palavras hifenizadas no fim de linha (ex:
"compre-ensão") preservadas de forma incorreta.

O script já corrige isso automaticamente: antes de traduzir, junta linhas
consecutivas do PDF (que são apenas quebra visual de coluna) numa única
sequência de texto corrido, removendo a hifenização de quebra de linha. Só
respeita como quebra de parágrafo real as linhas em branco já presentes na
extração.

<details>
<summary><strong>Nota técnica: por que não detectar parágrafos por comprimento de linha</strong></summary>

**Nota técnica:** uma primeira versão tentava detectar automaticamente o fim
de cada parágrafo por comprimento de linha ("linha bem mais curta que o padrão
da página"). Isso pareceu funcionar em testes sintéticos, mas falhou em PDFs
reais — título, autores e corpo de texto têm larguras de linha bem diferentes
na mesma página, o que causava fragmentação excessiva (um "parágrafo" por
frase) e, como efeito colateral, um aumento de mais de 10x no número de
chamadas à API (cada fragmento pequeno virava uma chamada separada ao Ollama).
A abordagem atual, mais simples, evita esse problema: junta tudo que não tem
quebra em branco explícita, mesmo que isso ocasionalmente junte um
título/lista de autores ao parágrafo seguinte em vez de mantê-los separados.

</details>

<details>
<summary><strong>📊 Qual tamanho de modelo escolher — benchmarks completos (clique para expandir)</strong></summary>

## Qual tamanho de modelo escolher

Testamos os três tamanhos traduzindo o mesmo artigo acadêmico completo (~15.600
palavras, 22 páginas) numa GPU de 8 GB (RTX 4060). Resultado:

| Modelo | Tempo total | Tempo médio/chamada | CPU/GPU | Observações |
|---|---|---|---|---|
| `4b` | ~7,2 min | ~10s | 100% GPU | Roda inteiro na GPU. Rápido e estável. |
| `12b` | ~49,6 min | ~74s | 39%/61% CPU/GPU | Não cabe inteiro na VRAM, mas fica razoavelmente equilibrado. |
| `27b` | ~3h 5,6min | ~464s | 71%/29% CPU/GPU | Maior parte do processamento em CPU. Só 24 chamadas (vs. 43 do `4b`), mas cada uma bem mais lenta -- ainda assim o mais completo em preservar notas de rodapé/legendas. |

O tempo do `12b`/`27b` **varia bastante** conforme o que mais estiver disputando
VRAM no momento (navegador com muitas abas, outros programas) — chegamos a medir
o `12b` entre ~50 e ~80 minutos no mesmo documento, dependendo disso. Fechar
aplicativos que usam GPU antes de rodar pode reduzir o tempo total
significativamente nesses modelos.

Em um teste anterior com um trecho curto (abstract + introdução), o `27b`
traduziu de forma mais completa e consistente que o `4b` — e no teste com o
documento inteiro (tabela acima) confirmou essa vantagem, preservando conteúdo
(nota de rodapé do autor, legenda de figura) que as extrações mais antigas com
`4b`/`12b` haviam perdido. Já o `12b`, num teste anterior, não repetiu essa
vantagem: cometeu um erro de pontuação e foi inconsistente na tradução de uma
sigla técnica (misturou "NLU" e "CLN" no mesmo documento, enquanto o `4b`
manteve consistência). Ou seja, mais parâmetros nem sempre significa tradução
melhor na prática — depende do trecho e de como o modelo se comporta rodando
parcialmente em CPU.

Recomendação prática, nessa faixa de hardware (GPU de 8 GB):

- **`4b`** — modelo padrão para a maioria dos casos. Rápido, roda inteiro na
  GPU, e não apresentou perda de completude nos testes com o documento inteiro.
- **`12b` / `27b`** — reserve para quando a completude/terminologia importar
  mais que a velocidade (ex: material acadêmico denso, com muitas notas de
  rodapé e citações). Espere tempos bem maiores -- o `27b` levou ~3h para o
  mesmo documento que o `4b` traduz em ~7 minutos. Sempre use `--timeout`
  generoso (veja abaixo).

Se sua GPU tiver VRAM suficiente para rodar `12b` ou `27b` inteiramente nela
(sem cair para CPU), os tempos acima não se aplicam — nesse caso os modelos
maiores tendem a compensar mais o custo extra de tempo.

### Timeout: quando é necessário usar `--timeout`

A biblioteca `polyglot-gpu` usa, por padrão, um timeout de 60 segundos por
chamada ao Ollama. Isso é suficiente para o `4b` (que gera cada chunk em
segundos), mas não para `12b`/`27b` rodando parcialmente em CPU, cujas chamadas
reais passam de 60s facilmente (observamos médias de ~74-121s no `12b` e
~464s no `27b`).

```bash
# 4b: não precisa de --timeout
python translate_pdf.py input/artigo.pdf --source en --target pt --model-size 4b

# 12b: --timeout generoso
python translate_pdf.py input/artigo.pdf --source en --target pt --model-size 12b --timeout 600

# 27b: --timeout ainda mais generoso
python translate_pdf.py input/artigo.pdf --source en --target pt --model-size 27b --timeout 1200
```

**Importante:** use um valor de `--timeout` bem acima da média observada, não
igual a ela. O tempo por chamada varia bastante conforme o conteúdo do trecho
(uma tabela densa demora mais que um parágrafo de prosa comum) e conforme a
carga da primeira chamada, que inclui o tempo de carregar o modelo na
memória. Na prática, `--timeout 600` funcionou bem para o `12b` (média ~74s,
folga de ~8x), mas falhou para o `27b` (média ~464s, folga insuficiente); só
`--timeout 1200` (folga de ~2,6x sobre a média) foi suficiente nesse caso.
Prefira errar para cima.

**Bug encontrado e corrigido:** a biblioteca original tem uma falha que faz
qualquer chamada falhar de forma rápida e imprevisível, mesmo com `--timeout`
alto — incluindo no `4b`. Antes de cada tradução, ela faz uma checagem rápida
("o modelo já está baixado?") usando um timeout curto de 10s, e depois
**reaproveita essa mesma conexão HTTP** para a chamada de tradução real, que
por sua vez herda silenciosamente esse limite de 10s em vez do timeout
configurado. O script corrige isso internamente (via monkey patch, aplicado
sempre, automaticamente), recriando a conexão do zero antes de cada chamada de
tradução com o timeout correto. Não é necessário fazer nada a mais — a correção
já está embutida em `translator_core.py`, usada tanto pelo CLI quanto pela
interface web.

### VRAM necessária

| Modelo | VRAM necessária | GPU mínima recomendada |
|---|---|---|
| `4b` | ~3.3 GB | Qualquer GPU com 4 GB+ de VRAM livre |
| `12b` | ~8.1 GB | GPU com 8 GB+ de VRAM livre (pouca folga) |
| `27b` | ~17 GB | GPU com 20 GB+ de VRAM livre |

Antes de escolher `12b` ou `27b`, confira quanta VRAM está livre:

```bash
nvidia-smi
```

Se outros programas (navegador, editores, etc.) já estiverem ocupando boa
parte da VRAM, feche-os antes de rodar o script, ou use um modelo menor. Se o
modelo não couber inteiramente na VRAM, o Ollama descarrega parte dele para a
CPU/RAM do sistema, o que é a causa dos tempos muito mais altos mostrados
acima.

</details>

## Gerar PDF (opcional)

Além do `.md`, o CLI e a interface web podem gerar um `.pdf` correspondente,
usando [pandoc](https://pandoc.org) com o motor
[wkhtmltopdf](https://wkhtmltopdf.org). É uma dependência **opcional** -- só
necessária se você usar essa função; a tradução normal (`.md`) não depende
dela.

**Instalação** (uma vez só):
1. [Instale o pandoc](https://pandoc.org/installing.html)
2. [Instale o wkhtmltopdf](https://wkhtmltopdf.org/downloads.html)
3. Confirme que os dois estão no PATH: `pandoc --version` e `wkhtmltopdf --version`

**CLI:**
```bash
python translate_pdf.py input/artigo.pdf --source en --target pt --to-pdf
```
Gera `output/artigo.4b.md` **e** `output/artigo.4b.pdf`.

**Interface web:** marque a caixa "Também gerar PDF" em "Opções avançadas"
antes de clicar em Traduzir.

Se o pandoc/wkhtmltopdf não estiverem instalados, a tradução em `.md` é
concluída normalmente de qualquer forma -- só a conversão extra falha, com
uma mensagem clara indicando o que instalar.

## Gerar HTML (opcional)

Também é possível gerar um `.html` autocontido (CSS embutido, um único
arquivo, abre em qualquer navegador). Ao contrário do PDF, **não precisa de
nenhum programa externo** -- só a biblioteca Python `markdown`, já incluída
no `requirements.txt`.

**CLI:**
```bash
python translate_pdf.py input/artigo.pdf --source en --target pt --to-html
```

**Interface web:** marque a caixa "Também gerar HTML".

Como não depende de nada externo, é a opção mais simples das duas caso você
só queira algo mais legível que o `.md` puro sem instalar mais nada.

## Roadmap

- [ ] Suporte a OCR automático para PDFs escaneados
- [ ] Suporte a `.docx` como entrada
- [ ] Cache de tradução para reprocessamento incremental
- [ ] Cancelar tradução em andamento na interface web

## Contribuindo

Sugestões, correções e pull requests são bem-vindos. Abra uma issue descrevendo
o problema ou a melhoria antes de submeter mudanças maiores.

## Licença

MIT — veja [LICENSE](LICENSE).
