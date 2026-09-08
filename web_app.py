"""
web_app.py

Interface web (Streamlit) para o pdf-translator: página única, sem login,
100% local. A tradução roda numa thread em segundo plano, independente do
ciclo de execução do Streamlit -- ela continua mesmo se você trocar de aba
do NAVEGADOR ou a página recarregar. Isso NÃO vale para o terminal: fechar
a janela do PowerShell onde este script está rodando mata o processo
inteiro, incluindo qualquer tradução em andamento.

O estado de cada tradução é persistido em arquivos JSON em disco (não em
memória), o que permite reconectar corretamente ao progresso mesmo após um
F5 no meio de uma tradução longa. Logs detalhados vão para web_app.log.

Uso:
    streamlit run web_app.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import streamlit as st

from translator_core import (
    HtmlConversionError,
    PdfConversionError,
    Progress,
    TranslationCancelledError,
    build_output_markdown,
    convert_markdown_to_html,
    convert_markdown_to_pdf,
    default_output_path,
    parse_page_range,
    translate_document,
)

# --------------------------------------------------------------------------
# Logging persistente -- grava em web_app.log (na pasta do projeto) E no
# terminal, com timestamp, cobrindo tanto o script principal quanto a thread
# de tradução em segundo plano (que hoje não tinha NENHUMA visibilidade se
# travasse ou falhasse antes de conseguir atualizar o status). Isso permite
# diagnosticar problemas olhando o arquivo de log diretamente, sem depender
# de capturar mensagens que aparecem e desaparecem na página.
#
# O guard "if not logger.handlers" evita duplicar handlers a cada rerun do
# Streamlit (que reexecuta este arquivo inteiro do zero a cada interação).
# --------------------------------------------------------------------------

logger = logging.getLogger("pdf_translator_web")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _formatter = logging.Formatter("%(asctime)s [%(threadName)s] %(levelname)s: %(message)s")

    _file_handler = logging.FileHandler("web_app.log", encoding="utf-8")
    _file_handler.setFormatter(_formatter)
    logger.addHandler(_file_handler)

    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(_formatter)
    logger.addHandler(_console_handler)

    # translator_core.py usa warnings.warn() para avisos operacionais (ex: o
    # recuo automático da detecção de cabeçalhos quando o resultado parece
    # suspeito). Por padrão isso só vai pro stderr do processo; redirecionamos
    # também para o mesmo log em arquivo, consistente com todo o resto.
    logging.captureWarnings(True)
    _py_warnings_logger = logging.getLogger("py.warnings")
    _py_warnings_logger.setLevel(logging.WARNING)
    _py_warnings_logger.addHandler(_file_handler)
    _py_warnings_logger.addHandler(_console_handler)

UPLOAD_DIR = Path(tempfile.gettempdir()) / "pdf-translator-web"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Timeout sugerido por tamanho de modelo, baseado nos benchmarks reais medidos
# neste projeto (ver README) -- só um ponto de partida, o usuário pode ajustar.
SUGGESTED_TIMEOUT = {"4b": None, "12b": 600, "27b": 1200}

# --------------------------------------------------------------------------
# Estado do job: persistido em ARQUIVOS JSON em disco, não em memória.
#
# Tentamos originalmente guardar isso num dict Python em nível de módulo
# (protegido por lock), mas descobrimos via os logs que esse estado não
# sobrevivia de forma confiável entre execuções sucessivas do script neste
# ambiente -- o job era criado corretamente, mas sumia já na execução
# seguinte (logo após st.rerun()). Gravar em disco elimina esse problema por
# completo: não depende de nenhuma variável Python persistir entre reruns,
# só de arquivos existirem no disco, o que é sempre confiável.
# --------------------------------------------------------------------------

JOBS_DIR = UPLOAD_DIR / "jobs"
JOBS_DIR.mkdir(parents=True, exist_ok=True)
_LATEST_POINTER_FILE = JOBS_DIR / "_latest.txt"

# Protege o ciclo leitura-modificação-escrita de _set_job contra condição de
# corrida entre a thread principal do Streamlit (que escreve cancel_requested
# ao clicar em "Cancelar") e a thread de tradução em segundo plano (que
# escreve o progresso a cada chamada ao Ollama). Sem isso, as duas podem ler
# o arquivo "ao mesmo tempo" (antes da outra escrever), e quem escrever por
# último apaga sem querer a mudança da outra -- foi exatamente isso que
# fazia o cancelamento "sumir": a thread de tradução, escrevendo progresso
# com muita frequência, reescrevia por cima do cancel_requested=True antes
# da checagem seguinte conseguir vê-lo.
_JOB_FILE_LOCK = threading.Lock()


def _job_file(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.json"


def _set_job(job_id: str, **updates) -> None:
    path = _job_file(job_id)
    with _JOB_FILE_LOCK:
        current: dict = {}
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                current = {}
        current.update(updates)
        # Registra sempre quando foi a última escrita -- usado para detectar
        # jobs "fantasma" (status="running" travado para sempre porque o
        # processo que fazia a tradução morreu no meio, ex: terminal fechado
        # à força). Sem isso, reabrir a interface volta a mostrar eternamente
        # "traduzindo...", mesmo que a thread real já não exista mais.
        current["last_update"] = time.time()
        # Escreve num arquivo temporário e renomeia por cima (atômico no
        # mesmo sistema de arquivos).
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(current), encoding="utf-8")
        tmp_path.replace(path)


def _get_job(job_id: str) -> Optional[dict]:
    path = _job_file(job_id)
    with _JOB_FILE_LOCK:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None


def _set_latest_job_id(job_id: Optional[str]) -> None:
    if job_id is None:
        _LATEST_POINTER_FILE.unlink(missing_ok=True)
        return
    _LATEST_POINTER_FILE.write_text(job_id, encoding="utf-8")


def _get_latest_job_id() -> Optional[str]:
    if not _LATEST_POINTER_FILE.exists():
        return None
    content = _LATEST_POINTER_FILE.read_text(encoding="utf-8").strip()
    return content or None


def _run_translation_job(
    job_id: str,
    pdf_path: Path,
    original_filename: str,
    source: str,
    target: str,
    model_size: str,
    page_range,
    chunk_size: Optional[int],
    timeout: Optional[float],
    generate_pdf: bool = False,
    generate_html: bool = False,
    detect_headings: bool = False,
) -> None:
    """Executa a tradução numa thread separada. Roda seu próprio event loop
    asyncio (independente do event loop principal do Streamlit).

    'pdf_path' é o arquivo temporário (nome UUID) onde o upload foi salvo --
    usado só para a extração em si. 'original_filename' é o nome real do PDF
    enviado pelo usuário, usado para nomear a saída (mesma convenção do CLI:
    nome_do_pdf.<model-size>.md), em vez de herdar o nome UUID do temporário.
    """
    logger.info(f"[job {job_id}] Thread iniciada. pdf_path={pdf_path} model={model_size} timeout={timeout}")
    original_stem = Path(original_filename).stem
    _last_logged_pct_bucket = {"value": -1}

    def on_progress(progress: Progress) -> None:
        pct_bucket = (progress.pct // 20) * 20
        if pct_bucket != _last_logged_pct_bucket["value"]:
            logger.info(f"[job {job_id}] progresso: {progress.done}/{progress.estimated_total} ({progress.pct}%)")
            _last_logged_pct_bucket["value"] = pct_bucket
        _set_job(
            job_id,
            done=progress.done,
            estimated_total=progress.estimated_total,
            overflowed=progress.overflowed,
            pct=progress.pct,
            elapsed_s=progress.elapsed_s,
            eta_s=progress.eta_s,
        )

    try:
        logger.info(f"[job {job_id}] Chamando translate_document()...")
        result = asyncio.run(
            translate_document(
                pdf_path=pdf_path,
                source=source,
                target=target,
                model_size=model_size,
                page_range=page_range,
                chunk_size=chunk_size,
                timeout=timeout,
                progress_callback=on_progress,
                detect_headings=detect_headings,
                cancel_check=lambda: bool((_get_job(job_id) or {}).get("cancel_requested")),
            )
        )
        logger.info(f"[job {job_id}] translate_document() retornou com sucesso. {result.ollama_calls} chamadas, {result.elapsed_s:.1f}s")
        final_markdown = build_output_markdown(
            original_stem, source, target, f"translategemma:{model_size}", result.markdown
        )

        # Grava em disco automaticamente (igual o CLI faz), além de guardar em
        # memória para o botão de download -- assim, mesmo que a interface
        # "perca o fio" (sessão do navegador reiniciada, aba fechada por
        # engano, etc.), o resultado nunca fica só na memória: já está salvo
        # em output/ quando a tradução termina. Usa o nome original do PDF
        # (não o UUID do arquivo temporário), igual o CLI: nome.<model>.md
        out_path = default_output_path(Path(original_filename), model_size)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(final_markdown, encoding="utf-8")
        logger.info(f"[job {job_id}] Salvo em disco: {out_path}")

        pdf_saved_path: Optional[str] = None
        pdf_error: Optional[str] = None
        if generate_pdf:
            pdf_out_path = out_path.with_suffix(".pdf")
            logger.info(f"[job {job_id}] Convertendo para PDF: {pdf_out_path}")
            try:
                convert_markdown_to_pdf(out_path, pdf_out_path, title=original_stem)
                pdf_saved_path = str(pdf_out_path)
                logger.info(f"[job {job_id}] PDF salvo em: {pdf_out_path}")
            except PdfConversionError as exc:
                pdf_error = str(exc)
                logger.warning(f"[job {job_id}] Conversão para PDF falhou: {exc}")

        html_saved_path: Optional[str] = None
        html_error: Optional[str] = None
        if generate_html:
            html_out_path = out_path.with_suffix(".html")
            logger.info(f"[job {job_id}] Convertendo para HTML: {html_out_path}")
            try:
                html_content = convert_markdown_to_html(final_markdown, title=original_stem)
                html_out_path.write_text(html_content, encoding="utf-8")
                html_saved_path = str(html_out_path)
                logger.info(f"[job {job_id}] HTML salvo em: {html_out_path}")
            except HtmlConversionError as exc:
                html_error = str(exc)
                logger.warning(f"[job {job_id}] Conversão para HTML falhou: {exc}")

        _set_job(
            job_id,
            status="done",
            pct=100,
            markdown=final_markdown,
            ollama_calls=result.ollama_calls,
            elapsed_s=result.elapsed_s,
            word_count=result.word_count,
            out_filename=f"{original_stem}.{model_size}.md",
            saved_path=str(out_path),
            pdf_filename=f"{original_stem}.{model_size}.pdf" if pdf_saved_path else None,
            pdf_saved_path=pdf_saved_path,
            pdf_error=pdf_error,
            html_filename=f"{original_stem}.{model_size}.html" if html_saved_path else None,
            html_saved_path=html_saved_path,
            html_error=html_error,
        )
        logger.info(f"[job {job_id}] Status atualizado para 'done'.")
    except TranslationCancelledError:
        logger.info(f"[job {job_id}] Tradução cancelada pelo usuário.")
        _set_job(job_id, status="cancelled")
    except Exception as exc:  # noqa: BLE001 -- queremos capturar e mostrar qualquer erro na UI
        logger.exception(f"[job {job_id}] ERRO durante a tradução:")
        _set_job(job_id, status="error", error=str(exc))


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------

st.set_page_config(page_title="pdf-translator", page_icon="📄", layout="centered")
st.title("📄 pdf-translator")
st.caption("Tradução de PDFs 100% local, via Ollama + TranslateGemma -- nenhum dado sai do computador.")

if "job_id" not in st.session_state:
    # Sessão nova (por exemplo, depois de um F5) -- reconecta automaticamente
    # à última tradução iniciada nesta máquina, em vez de mostrar a tela como
    # se nada estivesse acontecendo enquanto ela roda (ou já rodou) em segundo
    # plano no servidor.
    st.session_state.job_id = _get_latest_job_id()

active_job = _get_job(st.session_state.job_id) if st.session_state.job_id else None
is_running = active_job is not None and active_job.get("status") == "running"

# --- Formulário (desabilitado enquanto uma tradução está em andamento) -----
# Nota: não usamos st.form aqui de propósito -- widgets soltos + um botão
# comum (st.button) são mais simples e evitam qualquer comportamento
# específico de formulário que possa interferir na comunicação com o servidor.

uploaded_file = st.file_uploader("PDF de entrada", type=["pdf"], disabled=is_running)

if uploaded_file is not None:
    # A key inclui nome+tamanho do arquivo, então trocar de PDF reseta esse
    # campo para o novo nome detectado -- editar o texto não é perdido só
    # por causa de um rerun do Streamlit (só quando o arquivo muda de verdade).
    output_name = st.text_input(
        "Nome do arquivo de saída (opcional)",
        value=Path(uploaded_file.name).stem,
        key=f"output_name_{uploaded_file.name}_{uploaded_file.size}",
        disabled=is_running,
        help=(
            "Detectado automaticamente a partir do nome do arquivo enviado. "
            "Se aparecer estranho ou truncado (ex: nomes curtos estilo "
            "Windows, tipo 'ABCDEF~1'), corrija aqui -- isso não afeta a "
            "tradução em si, só o nome do arquivo salvo em output/."
        ),
    )
else:
    output_name = ""

col1, col2 = st.columns(2)
with col1:
    source = st.text_input("Idioma de origem", value="en", disabled=is_running)
with col2:
    target = st.text_input("Idioma de destino", value="pt", disabled=is_running)

model_size = st.selectbox(
    "Tamanho do modelo",
    options=["4b", "12b", "27b"],
    index=0,
    disabled=is_running,
    help=(
        "4b: rápido, roda inteiro na GPU na maioria dos casos. "
        "12b/27b: mais lento (pode levar horas em GPUs com pouca VRAM), "
        "mas mais completo/consistente em documentos longos -- veja o README."
    ),
)

pages = st.text_input(
    "Páginas (opcional)", value="", placeholder="ex: 1-10 (deixe vazio para todas)", disabled=is_running
)

with st.expander("Opções avançadas"):
    suggested = SUGGESTED_TIMEOUT.get(model_size)
    timeout = st.number_input(
        "Timeout por chamada ao Ollama (segundos)",
        min_value=0,
        value=suggested or 0,
        step=30,
        disabled=is_running,
        help=(
            "0 = usa o padrão da biblioteca (60s), suficiente só para o 4b. "
            "Para 12b/27b, use um valor bem acima da média observada -- "
            "veja a seção 'Timeout' do README para os números reais medidos."
        ),
    )
    chunk_size = st.number_input(
        "Tamanho do chunk (caracteres, opcional)",
        min_value=0,
        value=0,
        step=500,
        disabled=is_running,
        help="0 = automático, conforme o tamanho do modelo (2000/4000/6000).",
    )
    generate_pdf = st.checkbox(
        "Também gerar PDF",
        value=False,
        disabled=is_running,
        help=(
            "Além do .md, converte o resultado para .pdf usando pandoc + wkhtmltopdf. "
            "Requer os dois instalados e no PATH (https://pandoc.org e "
            "https://wkhtmltopdf.org) -- se não estiverem, a tradução ainda é "
            "concluída normalmente, só a conversão extra falha."
        ),
    )
    generate_html = st.checkbox(
        "Também gerar HTML",
        value=False,
        disabled=is_running,
        help=(
            "Além do .md, converte o resultado para .html autocontido. Não "
            "precisa de nenhum programa externo, só a biblioteca Python "
            "'markdown' (já incluída em requirements.txt)."
        ),
    )
    detect_headings = st.checkbox(
        "Detectar títulos/subtítulos (experimental)",
        value=False,
        disabled=is_running,
        help=(
            "Tenta reconstruir títulos/subtítulos como cabeçalhos Markdown "
            "reais, pelo tamanho da fonte no PDF, em vez de tratar tudo como "
            "texto corrido. Pode ocasionalmente classificar errado uma linha "
            "(ex: autores) como cabeçalho de baixo nível, se ela tiver o "
            "mesmo tamanho de fonte de um subtítulo real -- veja o README "
            "para detalhes. Desativado por padrão."
        ),
    )

submitted = st.button("Traduzir", disabled=is_running, use_container_width=True, type="primary")

if submitted:
    logger.info(f"Botão 'Traduzir' clicado. uploaded_file={uploaded_file!r}")
    if uploaded_file is None:
        logger.warning("Clique processado, mas nenhum arquivo foi enviado.")
        st.warning("Envie um PDF antes de traduzir.")
    else:
        try:
            job_id = str(uuid.uuid4())
            pdf_path = UPLOAD_DIR / f"{job_id}.pdf"
            pdf_path.write_bytes(uploaded_file.getvalue())
            logger.info(f"[job {job_id}] PDF salvo em {pdf_path} ({pdf_path.stat().st_size} bytes)")

            # Usa o nome customizado pelo usuário (se preenchido) em vez do
            # nome bruto reportado pelo navegador -- protege contra casos em
            # que o SO/navegador reporta um "apelido" curto/truncado (ex:
            # nomes estilo Windows 8.3, tipo "ABCDEF~1") em vez do nome real.
            effective_filename = f"{output_name.strip()}.pdf" if output_name and output_name.strip() else uploaded_file.name

            _set_job(
                job_id,
                status="running",
                done=0,
                estimated_total=1,
                overflowed=False,
                pct=0,
                elapsed_s=0.0,
                eta_s=None,
            )
            st.session_state.job_id = job_id
            _set_latest_job_id(job_id)
            logger.info(f"[job {job_id}] Job registrado, iniciando thread de tradução...")

            thread = threading.Thread(
                target=_run_translation_job,
                kwargs=dict(
                    job_id=job_id,
                    pdf_path=pdf_path,
                    original_filename=effective_filename,
                    source=source,
                    target=target,
                    model_size=model_size,
                    page_range=parse_page_range(pages or None),
                    chunk_size=(chunk_size or None),
                    timeout=(timeout or None),
                    generate_pdf=generate_pdf,
                    generate_html=generate_html,
                    detect_headings=detect_headings,
                ),
                daemon=True,
                name=f"translate-{job_id[:8]}",
            )
            thread.start()
            logger.info(f"[job {job_id}] Thread iniciada (thread.is_alive()={thread.is_alive()}). Chamando st.rerun()...")
            st.rerun()
        except Exception:
            logger.exception("ERRO ao tentar iniciar a tradução (antes de chegar na thread):")
            raise

# --- Status (sempre visível, para nunca deixar dúvida sobre o que está acontecendo) ---

st.divider()
st.subheader("Status")
status_box = st.container(border=True)

with status_box:
    if active_job is None:
        st.caption("💤 Nenhuma tradução em andamento. Envie um PDF acima e clique em **Traduzir**.")

    else:
        status = active_job.get("status")

        if status == "running":
            last_update = active_job.get("last_update", 0)
            staleness_s = time.time() - last_update
            STALE_THRESHOLD_S = 30 * 60  # 30 minutos sem nenhuma atualização = provável travamento

            if staleness_s > STALE_THRESHOLD_S:
                st.warning(
                    "⚠️ Esta tradução parece ter parado inesperadamente "
                    f"(sem nenhuma atualização há {staleness_s / 60:.0f} minutos). "
                    "Isso costuma acontecer se o terminal rodando o Streamlit foi "
                    "fechado no meio da tradução -- o processo é encerrado junto, "
                    "sem deixar erro registrado. A tradução em si precisa ser refeita."
                )
                if st.button("Descartar e começar uma nova tradução"):
                    st.session_state.job_id = None
                    _set_latest_job_id(None)
                    st.rerun()
            else:
                pct = active_job.get("pct", 0)
                done = active_job.get("done", 0)
                total = active_job.get("estimated_total", 1)
                overflowed = active_job.get("overflowed", False)
                elapsed = active_job.get("elapsed_s", 0.0)
                eta = active_job.get("eta_s")
                cancel_requested = active_job.get("cancel_requested", False)

                if cancel_requested:
                    st.info(
                        "⏹️ Cancelamento solicitado -- aguardando o fim da chamada "
                        "atual ao Ollama (o cancelamento não é instantâneo; com "
                        "modelos maiores, uma única chamada pode levar vários "
                        "minutos)..."
                    )
                elif done == 0:
                    st.info("⏳ Iniciando -- extraindo texto do PDF e conectando ao Ollama...")
                else:
                    st.info("🔄 Traduzindo... você pode deixar esta ABA DO NAVEGADOR aberta e usar outras abas -- a tradução continua no servidor (mas não feche o terminal do PowerShell).")

                st.progress(pct / 100)
                total_display = f"{done}/~{total}+" if overflowed else f"{done}/{total}"
                eta_display = "calculando..." if overflowed or eta is None else f"{eta:.0f}s"
                st.caption(f"{total_display} chamadas ao Ollama • decorrido: {elapsed:.0f}s • restante estimado: {eta_display}")

                if not cancel_requested:
                    if st.button(
                        "⏹️ Cancelar tradução",
                        help=(
                            "O cancelamento não é instantâneo: só surte efeito depois "
                            "que a chamada em andamento ao Ollama terminar. O .md "
                            "parcial NÃO é salvo -- a tradução precisa ser refeita do "
                            "zero caso você queira o documento completo depois."
                        ),
                    ):
                        _set_job(st.session_state.job_id, cancel_requested=True)
                        st.rerun()

                time.sleep(2)
                st.rerun()

        elif status == "done":
            st.success(
                f"✅ Tradução concluída em {active_job['elapsed_s']:.1f}s "
                f"({active_job['ollama_calls']} chamada(s) ao Ollama, ~{active_job['word_count']} palavras). "
                f"Salvo automaticamente em `{active_job.get('saved_path', 'output/')}`."
            )
            st.download_button(
                "⬇️ Baixar tradução (.md)",
                data=active_job["markdown"].encode("utf-8"),
                file_name=active_job["out_filename"],
                mime="text/markdown",
                use_container_width=True,
            )

            if active_job.get("pdf_saved_path"):
                pdf_file_path = Path(active_job["pdf_saved_path"])
                if pdf_file_path.exists():
                    st.download_button(
                        "⬇️ Baixar tradução (.pdf)",
                        data=pdf_file_path.read_bytes(),
                        file_name=active_job["pdf_filename"],
                        mime="application/pdf",
                        use_container_width=True,
                    )
                else:
                    st.warning(
                        f"O arquivo PDF gerado não foi encontrado em `{pdf_file_path}` "
                        "(pode ter sido movido/apagado, ou você está rodando o app de "
                        "uma pasta diferente da que gerou este resultado)."
                    )
            elif active_job.get("pdf_error"):
                st.warning(f"PDF não gerado: {active_job['pdf_error']}")

            if active_job.get("html_saved_path"):
                html_file_path = Path(active_job["html_saved_path"])
                if html_file_path.exists():
                    st.download_button(
                        "⬇️ Baixar tradução (.html)",
                        data=html_file_path.read_bytes(),
                        file_name=active_job["html_filename"],
                        mime="text/html",
                        use_container_width=True,
                    )
                else:
                    st.warning(
                        f"O arquivo HTML gerado não foi encontrado em `{html_file_path}` "
                        "(pode ter sido movido/apagado, ou você está rodando o app de "
                        "uma pasta diferente da que gerou este resultado)."
                    )
            elif active_job.get("html_error"):
                st.warning(f"HTML não gerado: {active_job['html_error']}")

            with st.expander("Pré-visualizar"):
                st.markdown(active_job["markdown"][:5000])
                if len(active_job["markdown"]) > 5000:
                    st.caption("(pré-visualização truncada -- baixe o arquivo para ver o conteúdo completo)")

            if st.button("Nova tradução"):
                st.session_state.job_id = None
                _set_latest_job_id(None)
                st.rerun()

        elif status == "error":
            st.error(f"❌ Erro durante a tradução: {active_job.get('error')}")
            if st.button("Tentar novamente"):
                st.session_state.job_id = None
                _set_latest_job_id(None)
                st.rerun()

        elif status == "cancelled":
            st.warning("⏹️ Tradução cancelada. Nenhum arquivo foi salvo -- a tradução precisa ser refeita do zero, se quiser o documento completo.")
            if st.button("Nova tradução", key="new_translation_after_cancel"):
                st.session_state.job_id = None
                _set_latest_job_id(None)
                st.rerun()
