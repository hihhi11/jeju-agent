"""브라우저 화면. 질문을 받아 답변, 차트, 표, 근거와 한계를 보여 준다.

    run.bat   또는   python -m streamlit run app.py
"""

from __future__ import annotations

import logging

import duckdb
import streamlit as st

from agent.agent import Agent, AgentAnswer
from agent.center import CenterStore
from agent.common import ToolResult
from agent.display import (
    build_figure,
    chart_note,
    clarification_choices,
    count_columns,
    csv_bytes,
    evidence_lines,
    hub_frame,
    result_title,
    rows_frame,
    safe_markdown,
    visible_results,
)
from agent.llm import LLMError, current_model
from agent.tools import find_data

EXAMPLES = [
    "서귀포시 관광지는 어느 달에 방문객이 가장 많아?",
    "2024년 방문객 수가 코로나 이전인 2019년 수준을 회복한 관광지는 어디야?",
    "비 오는 날에는 평소보다 버스 승차 인원이 얼마나 줄어?",
    "레저 목적으로 제주에 들어오는 사람은 요즘 늘었어?",
    "2024년에 방문객이 가장 많았던 관광지 5곳은 어디야?",
    "제주 관광과 관련해서 보유하고 있는 데이터에는 어떤 것이 있어?",
]
# Streamlit 페이지는 영어로 표시되어 있어 브라우저 자동 번역이 한국어 문장을 망가뜨린다. 긴 예시 질문은 옆 메뉴에서도 줄바꿈한다
PAGE_SETUP = """
<script>
const root = document.documentElement;
root.lang = "ko";
root.setAttribute("translate", "no");
root.classList.add("notranslate");
if (!document.querySelector('meta[name="google"]')) {
  const meta = document.createElement("meta");
  meta.name = "google";
  meta.content = "notranslate";
  document.head.appendChild(meta);
}
</script>
<style>
[data-testid="stSidebar"] button p { white-space: normal; word-break: keep-all; overflow: visible; text-overflow: clip; }
[data-testid="stSidebar"] button div { overflow: visible; }
</style>
"""
ASK_VIEW, DATA_VIEW = "질문하기", "데이터 목록"
UNEXPECTED_ERROR_TEXT = "질문을 처리하다 문제가 생겼습니다. 다시 시도하거나 새 대화를 시작해 주세요."
CHART_CONFIG = {
    "displaylogo": False,
    "displayModeBar": True,
    "modeBarButtonsToRemove": ["zoom2d", "pan2d", "select2d", "lasso2d", "zoomIn2d", "zoomOut2d", "autoScale2d", "resetScale2d"],
    "toImageButtonOptions": {"format": "png", "filename": "jeju_chart", "scale": 2},
}

st.set_page_config(page_title="제주 관광 동향 분석", page_icon="🍊", layout="wide")


def _state():
    state = st.session_state
    state.setdefault("messages", [])
    state.setdefault("pending", None)
    # 센터 CSV는 파일로 저장하지 않고 이 세션의 메모리에만 둔다. 화면을 닫으면 사라진다
    state.setdefault("center_store", CenterStore())
    state.setdefault("center_loaded", set())
    state.setdefault("center_uploader", 0)
    state.setdefault("center_errors", [])
    return state


def _ask(question: str) -> None:
    _state().pending = question


def _submit_chat() -> None:
    question = st.session_state.chat_box.strip()
    if question:
        _ask(question)


def _reset() -> None:
    state = _state()
    state.messages = []
    state.pop("agent", None)


def show_result(tool: str, result: ToolResult, key: str) -> None:
    figure = build_figure(result)
    if figure is not None:
        st.plotly_chart(figure, theme=None, width="stretch", config=CHART_CONFIG, key=f"chart_{key}")
        note = chart_note(result)
        if note:
            st.caption(note)
    if result.rows:
        frame = rows_frame(result.rows)
        with st.expander(f"표 보기: {result_title(result)}"):
            formats = {c: st.column_config.NumberColumn(format="localized") for c in count_columns(frame)}
            st.dataframe(frame, hide_index=True, width="stretch", column_config=formats, key=f"table_{key}")
            st.download_button("CSV 내려받기", csv_bytes(frame), file_name=f"jeju_{tool}_{key}.csv", mime="text/csv", key=f"csv_{key}")
    hub = hub_frame(result)
    if hub is not None:
        with st.expander(f"제주데이터허브 목록에서 찾은 자료 {len(hub)}건"):
            st.caption("목록에 있어도 이 프로그램으로 분석할 수는 없습니다. 허브 목록은 화면에서만 볼 수 있습니다.")
            st.dataframe(hub, hide_index=True, width="stretch", key=f"hub_{key}")


def show_evidence(shown: list[tuple[str, ToolResult]]) -> None:
    """LLM이 근거나 한계를 빠뜨려도 볼 수 있게 도구가 준 값을 그대로 보여 준다."""
    if not any(r.evidence or r.limits for _, r in shown):
        return
    with st.expander("근거와 한계 (계산 결과 원문)"):
        for _, result in shown:
            lines = evidence_lines(result)
            if not lines and not result.limits:
                continue
            st.markdown(f"**{safe_markdown(result_title(result))}**")
            for label, value in lines:
                st.markdown(f"- {label}: {safe_markdown(value)}")
            for limit in result.limits:
                st.markdown(f"- 한계: {safe_markdown(limit)}")


def show_answer(answer: AgentAnswer, index: int, is_latest: bool) -> None:
    if answer.status == "llm_error":
        st.error(answer.text)
    elif answer.status != "ok":
        st.warning(answer.text)
    else:
        st.markdown(safe_markdown(answer.text))
    if answer.unverified:
        st.warning(f"계산 결과에서 확인되지 않은 값이 있습니다: {', '.join(answer.unverified)}. 이 값은 믿지 말고 아래 표와 대조해 주세요.")
    choices = clarification_choices(answer)
    if choices and is_latest:
        st.caption("아래에서 고르거나 직접 입력해 주세요.")
        for column, choice in zip(st.columns(min(len(choices), 4)) * (len(choices) // 4 + 1), choices):
            column.button(choice, key=f"choice_{index}_{choice}", on_click=_ask, args=(choice,), disabled=bool(_state().pending), width="stretch")
    shown = visible_results(answer)
    for number, (tool, result) in enumerate(shown):
        show_result(tool, result, f"{index}_{number}")
    show_evidence(shown)


def show_examples(prefix: str, columns: int) -> None:
    disabled = bool(_state().pending)
    slots = st.columns(columns)
    for number, example in enumerate(EXAMPLES):
        slots[number % columns].button(example, key=f"{prefix}_{number}", on_click=_ask, args=(example,), disabled=disabled, width="stretch")


def run_pending() -> None:
    state = _state()
    question = state.pending
    with st.chat_message("user"):
        st.markdown(safe_markdown(question))
    with st.chat_message("assistant"):
        with st.spinner("자료를 찾아 계산하고 있습니다. 혼잡할 때는 1분 넘게 걸릴 수 있습니다."):
            try:
                if "agent" not in state:
                    state.agent = Agent(center_store=state.center_store)
                answer = state.agent.ask(question)
            except LLMError as exc:
                answer = AgentAnswer(text=str(exc), status="llm_error")
            except Exception:
                # 여기서 멈추면 pending 이 남아 입력창이 잠긴 채로 같은 질문을 되풀이한다
                logging.exception("질문 처리 실패")
                answer = AgentAnswer(text=UNEXPECTED_ERROR_TEXT, status="llm_error")
    state.messages.append({"question": question, "answer": answer})
    state.pending = None
    st.rerun()


def ask_view() -> None:
    state = _state()
    if not state.messages and not state.pending:
        st.markdown("제주 관광 데이터에 대해 평소 말투로 물어보세요. 표와 차트, 계산 근거와 한계를 함께 보여 드립니다.")
        st.markdown("**이런 질문을 해 보세요**")
        show_examples("main", 2)
    for index, message in enumerate(state.messages):
        with st.chat_message("user"):
            st.markdown(safe_markdown(message["question"]))
        with st.chat_message("assistant"):
            show_answer(message["answer"], index, index == len(state.messages) - 1)
    st.chat_input("질문을 입력하세요", key="chat_box", on_submit=_submit_chat, disabled=bool(state.pending))
    if state.pending:
        run_pending()


def data_view() -> None:
    st.markdown("이 프로그램으로 바로 분석할 수 있는 자료입니다. 낱말로 찾으면 센터 전용 명세와 제주데이터허브 목록도 함께 찾습니다.")
    keyword = st.text_input("찾을 낱말", placeholder="예: 버스, 카드, 입도객", key="data_search").strip()
    result = find_data(keyword)
    if result.rows:
        st.subheader(f"분석할 수 있는 자료 {len(result.rows)}건")
        # 기간이 없는 자료(정류소 정보 등)는 칸을 비운다
        frame = rows_frame(result.rows)[["자료 이름", "단위", "시작", "끝", "설명", "주의사항"]].fillna("")
        st.dataframe(frame, hide_index=True, width="stretch", height="content")
    else:
        st.info("분석할 수 있는 자료에서 찾지 못했습니다.")
    centers = result.summary.get("centerMatches") or []
    if centers:
        st.subheader(f"제주빅데이터센터 전용 명세 {result.summary['centerMatchCount']}건 중 {len(centers)}건")
        st.caption("명세만 있어 값을 계산할 수 없습니다.")
        st.dataframe(rows_frame(centers), hide_index=True, width="stretch")
    hub = hub_frame(result)
    if hub is not None:
        st.subheader(f"제주데이터허브 목록 {len(hub)}건")
        st.caption("목록에 있어도 이 프로그램으로 분석할 수는 없습니다. 허브 목록은 화면에서만 볼 수 있습니다.")
        st.dataframe(hub, hide_index=True, width="stretch")
    elif keyword and not result.summary["hubList"]["available"]:
        st.caption(result.summary["hubList"]["guide"])


def _load_center_files() -> None:
    """올린 파일 중 아직 넣지 않은 것만 확인해 넣는다. 오류는 다음 화면에 보여 준다."""
    state = _state()
    files = st.session_state.get(f"center_files_{state.center_uploader}") or []
    errors = []
    for file in files:
        if file.file_id in state.center_loaded:
            continue
        state.center_loaded.add(file.file_id)
        try:
            state.center_store.load(file.name, file.getvalue())
        except ValueError as exc:
            errors.append(str(exc))
        except Exception:
            logging.exception("센터 CSV 읽기 실패")
            errors.append(f"{file.name}: 파일을 읽지 못했습니다. 센터에서 내려받은 CSV인지 확인해 주세요.")
    state.center_errors = errors


def _clear_center() -> None:
    state = _state()
    state.center_store.clear()
    state.center_loaded = set()
    state.center_errors = []
    # 올린 파일 목록도 비우려고 새 업로드 칸을 만든다
    state.center_uploader += 1


def center_panel(busy: bool) -> None:
    state = _state()
    with st.expander("센터 CSV 넣기", expanded=bool(state.center_store.files)):
        st.caption("제주빅데이터센터에서 내려받은 카드 이용 현황(읍면동)_v02, 단기체류 외국인 유동인구(읍면동)_v02 CSV를 넣으면 질문에 함께 씁니다. 파일은 저장하지 않고 이 화면을 닫으면 지워집니다. 센터 반출 기준에 따라 사람 수가 10명 미만인 칸은 누구인지 짐작될 수 있어, 더 큰 단위(월→분기, 법정동→권역)로 묶거나 '값 가림'으로 표시합니다.")
        st.file_uploader("CSV 파일", type="csv", accept_multiple_files=True, key=f"center_files_{state.center_uploader}", on_change=_load_center_files, disabled=busy, label_visibility="collapsed")
        for error in state.center_errors:
            st.error(error)
        for item in state.center_store.summary():
            mark = " (가상 데이터)" if item["virtual"] else ""
            st.markdown(f"- {item['centerNo']} {item['title']}: {item['period']}, {item['months']}개월{mark}")
        if state.center_store.files:
            st.button("넣은 자료 지우기", key="center_clear", on_click=_clear_center, disabled=busy, width="stretch")


def sidebar() -> str:
    state = _state()
    busy = bool(state.pending)
    with st.sidebar:
        view = st.radio("화면", [ASK_VIEW, DATA_VIEW], disabled=busy, label_visibility="collapsed")
        st.button("새 대화 시작", key="reset", on_click=_reset, disabled=busy, width="stretch")
        center_panel(busy)
        if view == ASK_VIEW and state.messages:
            st.markdown("**예시 질문**")
            show_examples("side", 1)
        st.divider()
        st.caption("차트 오른쪽 위 카메라 버튼으로 그림을 저장할 수 있습니다.")
        try:
            provider, model = current_model()
        except LLMError as exc:
            st.error(str(exc))
            return view
        st.caption(f"사용 모델: {model or '지정 안 됨'}")
        if provider == "ollama":
            st.caption("질문과 계산 결과는 이 컴퓨터 안에서만 처리합니다.")
        else:
            st.caption("질문과 집계 결과가 모델 제공 서비스로 전송됩니다. 개인정보를 입력하지 마세요.")
    return view


def main() -> None:
    st.html(PAGE_SETUP, unsafe_allow_javascript=True)
    st.title("제주 관광 동향 분석")
    view = sidebar()
    try:
        if view == ASK_VIEW:
            ask_view()
        else:
            data_view()
    except duckdb.Error:
        st.error("데이터 파일을 열지 못했습니다. data/jeju.duckdb 가 있는지 확인해 주세요. 없으면 설치 안내대로 scripts/build_db.py 를 실행합니다.")


main()
