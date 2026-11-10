"""쇼핑 리스트 앱 (Streamlit)

추가 / 수정 / 삭제(되돌리기) / 체크 / 완료 항목 삭제 / 요약 정보를 제공한다.
데이터는 JSON 파일에 저장하며, 모든 변경은 "파일을 새로 읽고 → 변경 → 저장" 순서로 처리해
여러 탭·기기에서 동시에 써도 서로의 변경을 덮어쓰지 않도록 한다.
"""

import html
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import streamlit as st

# ---------------------------------------------------------------------------
# 설정값
# ---------------------------------------------------------------------------
# 환경변수 SHOPPING_LIST_FILE로 저장 파일 위치를 바꿀 수 있다 (테스트 시 실제 데이터와 분리하기 위함).
DATA_FILE = Path(os.environ.get("SHOPPING_LIST_FILE") or Path(__file__).parent / "shopping_list.json")
MAX_NAME_LENGTH = 50
MIN_QUANTITY = 1
MAX_QUANTITY = 999
DUPLICATE_ADD_WINDOW_SEC = 1.5  # 같은 내용이 이 시간 안에 다시 들어오면 더블클릭으로 보고 무시
EMPTY_MESSAGE = "목록이 비어 있어요. 사야 할 물건을 추가해 보세요!"


# ---------------------------------------------------------------------------
# 저장소 계층 (외부 저장소로 교체할 때 이 영역만 바꾸면 된다)
# ---------------------------------------------------------------------------
@st.cache_resource
def file_lock() -> threading.RLock:
    """모든 세션이 공유하는 파일 잠금. 읽기-수정-쓰기 사이에 다른 세션이 끼어들지 못하게 한다."""
    return threading.RLock()


def _backup_path(tag: str) -> Path:
    """손상 파일 보관용 경로 (예: shopping_list.corrupt-20261110-101500.json)."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return DATA_FILE.with_name(f"{DATA_FILE.stem}.{tag}-{stamp}{DATA_FILE.suffix}")


def _normalize_item(raw, seen_ids: set[str]) -> tuple[dict | None, bool]:
    """저장된 아이템 하나를 검사·보정한다.

    반환값: (보정된 아이템 또는 None(버림), 보정 여부)
    """
    if not isinstance(raw, dict):
        return None, True
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, True

    try:
        quantity = int(raw.get("quantity", MIN_QUANTITY))
    except (TypeError, ValueError):
        quantity = MIN_QUANTITY
    quantity = min(max(quantity, MIN_QUANTITY), MAX_QUANTITY)

    item_id = raw.get("id")
    if not isinstance(item_id, str) or not item_id or item_id in seen_ids:
        item_id = str(uuid.uuid4())
    seen_ids.add(item_id)

    item = {
        "id": item_id,
        "name": name.strip()[:MAX_NAME_LENGTH],
        "quantity": quantity,
        "checked": raw.get("checked") is True,
        "created_at": str(raw.get("created_at") or datetime.now().isoformat(timespec="seconds")),
    }
    return item, item != raw


def load_items() -> tuple[list[dict], str | None]:
    """저장 파일에서 아이템 목록을 읽는다.

    반환값: (아이템 목록, 경고 메시지 또는 None)
    - 파일이 없으면 빈 목록
    - JSON 자체가 깨졌으면 원본을 .corrupt-날짜 파일로 옮겨 보관하고 빈 목록
    - 일부 아이템 형식이 잘못됐으면 원본을 .backup-날짜 파일로 복사하고, 보정한 목록을 저장
    """
    if not DATA_FILE.exists():
        return [], None
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("최상위 데이터가 목록 형식이 아닙니다.")
    except (OSError, ValueError) as e:  # json.JSONDecodeError는 ValueError의 하위 클래스
        backup = _backup_path("corrupt")
        try:
            os.replace(DATA_FILE, backup)
            kept = f" 원본은 '{backup.name}'에 보관했어요."
        except OSError:
            kept = ""
        return [], f"저장된 목록을 읽지 못해 빈 목록으로 시작합니다.{kept} ({e})"

    items, changed, seen_ids = [], False, set()
    for raw in data:
        item, fixed = _normalize_item(raw, seen_ids)
        changed |= fixed
        if item is not None:
            items.append(item)

    if not changed:
        return items, None

    backup = _backup_path("backup")
    try:
        shutil.copy2(DATA_FILE, backup)
        save_items(items)
    except OSError:
        pass
    dropped = len(data) - len(items)
    return items, (
        f"저장된 목록 중 형식이 잘못된 항목을 보정했어요(제외 {dropped}개). "
        f"원본은 '{backup.name}'에 보관했어요."
    )


def save_items(items: list[dict]) -> None:
    """아이템 목록을 저장한다.

    임시 파일에 먼저 쓴 뒤 교체하여, 저장 도중 문제가 생겨도 기존 파일이 깨지지 않게 한다.
    """
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=DATA_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, DATA_FILE)
    except Exception:
        # 실패 시 임시 파일 정리 후 예외 전달
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


# ---------------------------------------------------------------------------
# 상태 관리
# ---------------------------------------------------------------------------
def init_state() -> None:
    """세션 상태 기본값을 만든다."""
    for key, default in {
        "shopping_items": [],
        "load_warning": None,
        "editing_id": None,
        "add_error": None,
        "edit_error": None,
        "save_error": None,
        "undo": None,  # {"entries": [(위치, 아이템), ...], "message": str}
        "last_add": None,  # (품목명, 수량, 시각) — 중복 추가 방지용
    }.items():
        st.session_state.setdefault(key, default)


def refresh_items() -> None:
    """매 실행마다 파일에서 최신 목록을 읽는다. 다른 탭에서 바뀐 내용도 반영된다."""
    with file_lock():
        items, warning = load_items()
    st.session_state.shopping_items = items
    if warning:
        st.session_state.load_warning = warning


def mutate(change: Callable[[list[dict]], None]) -> None:
    """최신 파일을 읽어 change를 적용하고 저장한다. 잠금 안에서 처리해 동시 수정에도 안전하다."""
    with file_lock():
        items, warning = load_items()
        if warning:
            st.session_state.load_warning = warning
        change(items)
        try:
            save_items(items)
            st.session_state.save_error = None
        except OSError as e:
            st.session_state.save_error = f"목록을 저장하지 못했습니다. ({e})"
    st.session_state.shopping_items = items


# ---------------------------------------------------------------------------
# 검증 / 헬퍼
# ---------------------------------------------------------------------------
def validate(name: str | None, quantity) -> tuple[str, int, str | None]:
    """입력값을 검증한다. 반환값: (정리된 품목명, 수량, 오류 메시지 또는 None)"""
    clean_name = (name or "").strip()
    if not clean_name:
        return clean_name, 0, "품목명을 입력해 주세요."
    if len(clean_name) > MAX_NAME_LENGTH:
        return clean_name, 0, f"품목명은 최대 {MAX_NAME_LENGTH}자까지 입력할 수 있어요. (지금 {len(clean_name)}자)"
    try:
        is_int = quantity is not None and float(quantity) == int(quantity)
    except (TypeError, ValueError):
        is_int = False
    if not is_int:
        return clean_name, 0, "수량은 정수로 입력해 주세요."
    qty = int(quantity)
    if not MIN_QUANTITY <= qty <= MAX_QUANTITY:
        return clean_name, 0, f"수량은 {MIN_QUANTITY}~{MAX_QUANTITY} 사이로 입력해 주세요."
    return clean_name, qty, None


def find_in(items: list[dict], item_id: str) -> dict | None:
    """목록에서 id로 아이템을 찾는다. 없으면 None."""
    return next((it for it in items if it["id"] == item_id), None)


def new_item(name: str, quantity: int) -> dict:
    """PRD 6장 데이터 모델에 맞는 아이템 딕셔너리를 생성한다."""
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "quantity": quantity,
        "checked": False,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# 아이템 조작 (모두 on_click / on_change 콜백으로 호출된다)
# ---------------------------------------------------------------------------
def add_item(name: str, quantity: int) -> None:
    """아이템을 목록 맨 아래에 추가한다."""
    st.session_state.undo = None
    mutate(lambda items: items.append(new_item(name, quantity)))


def update_item(item_id: str, name: str, quantity: int) -> None:
    """아이템의 품목명·수량을 수정한다. (다른 탭에서 이미 지워졌다면 아무것도 하지 않음)"""
    def change(items: list[dict]) -> None:
        item = find_in(items, item_id)
        if item is not None:
            item["name"] = name
            item["quantity"] = quantity

    st.session_state.undo = None
    mutate(change)


def _remove(predicate: Callable[[dict], bool], message: Callable[[list[dict]], str]) -> None:
    """조건에 맞는 아이템을 지우고, 되돌리기 정보를 남긴다."""
    removed: list[tuple[int, dict]] = []

    def change(items: list[dict]) -> None:
        removed.extend((i, it) for i, it in enumerate(items) if predicate(it))
        items[:] = [it for it in items if not predicate(it)]

    mutate(change)
    if removed:
        st.session_state.undo = {"entries": removed, "message": message([it for _, it in removed])}
    if st.session_state.editing_id and find_in(st.session_state.shopping_items, st.session_state.editing_id) is None:
        stop_edit()


def delete_item(item_id: str) -> None:
    """아이템 하나를 삭제한다."""
    _remove(lambda it: it["id"] == item_id, lambda its: f"'{its[0]['name']}'을(를) 삭제했어요.")


def clear_checked() -> None:
    """체크된 아이템을 모두 삭제한다."""
    _remove(lambda it: it["checked"], lambda its: f"완료 항목 {len(its)}개를 삭제했어요.")


def undo_delete() -> None:
    """마지막 삭제를 되돌린다. 원래 위치에 다시 넣는다."""
    undo = st.session_state.undo
    st.session_state.undo = None
    if not undo:
        return

    def change(items: list[dict]) -> None:
        existing = {it["id"] for it in items}
        for index, item in undo["entries"]:  # 앞쪽 위치부터 넣어야 원래 순서가 유지된다
            if item["id"] not in existing:
                items.insert(min(index, len(items)), item)

    mutate(change)


def toggle_item(item_id: str) -> None:
    """체크박스 위젯 값으로 아이템의 체크 상태를 맞춘다."""
    checked = bool(st.session_state[f"chk_{item_id}"])

    def change(items: list[dict]) -> None:
        item = find_in(items, item_id)
        if item is not None:
            item["checked"] = checked

    mutate(change)


# ---------------------------------------------------------------------------
# 폼 / 편집 모드 콜백
# ---------------------------------------------------------------------------
def handle_add() -> None:
    """추가 폼 제출 처리. 성공 시에만 입력칸을 비워, 실패 시 입력한 내용이 남도록 한다."""
    name, qty, error = validate(st.session_state.add_name, st.session_state.add_qty)
    st.session_state.add_error = error
    if error is not None:
        return

    # 더블클릭·Enter 연타로 같은 내용이 곧바로 다시 들어오면 무시한다.
    now = time.monotonic()
    last = st.session_state.last_add
    is_duplicate = last is not None and last[:2] == (name, qty) and now - last[2] < DUPLICATE_ADD_WINDOW_SEC
    if not is_duplicate:
        # 저장보다 먼저 기록해 둔다. (저장 도중 다음 요청으로 실행이 중단돼도 중복 판정이 가능하도록)
        st.session_state.last_add = (name, qty, now)
        add_item(name, qty)
    st.session_state.add_name = ""
    st.session_state.add_qty = MIN_QUANTITY


def start_edit(item_id: str) -> None:
    """편집 모드 진입. 다른 아이템을 편집 중이었다면 그 편집은 취소된다."""
    item = find_in(st.session_state.shopping_items, item_id)
    if item is None:
        return
    st.session_state.editing_id = item_id
    st.session_state.edit_error = None
    st.session_state[f"edit_name_{item_id}"] = item["name"]
    st.session_state[f"edit_qty_{item_id}"] = item["quantity"]


def stop_edit() -> None:
    """편집 모드 종료(취소)."""
    st.session_state.editing_id = None
    st.session_state.edit_error = None


def save_edit(item_id: str) -> None:
    """편집 내용 저장. 검증 실패 시 편집 모드를 유지한다."""
    name, qty, error = validate(
        st.session_state.get(f"edit_name_{item_id}"),
        st.session_state.get(f"edit_qty_{item_id}"),
    )
    if error:
        st.session_state.edit_error = error
        return
    update_item(item_id, name, qty)
    stop_edit()


# ---------------------------------------------------------------------------
# 화면 구성
# ---------------------------------------------------------------------------
# 라이트/다크 테마 모두에서 어울리도록 반투명 색상 위주로 지정한다.
CUSTOM_CSS = """
<style>
/* 전체 폭을 조금 좁혀 모바일 앱 같은 느낌으로 */
.block-container { max-width: 640px; padding-top: 2.5rem; }

/* 입력칸 위에 겹쳐 뜨는 "Press Enter to submit form" 안내 숨김 */
[data-testid="InputInstructions"] { display: none !important; }

/* 요약 카드 */
.summary-card { display: flex; gap: .5rem; margin: .25rem 0 .75rem; }
.summary-card .stat {
    flex: 1; text-align: center; padding: .6rem .25rem; border-radius: .75rem;
    background: rgba(128, 128, 128, .08);
}
.summary-card .stat.remain { background: rgba(59, 130, 246, .10); }
.summary-card .stat.done   { background: rgba(22, 163, 74, .12); }
.summary-card .num   { display: block; font-size: 1.5rem; font-weight: 700; line-height: 1.2; }
.summary-card .label { font-size: .8rem; opacity: .7; }

/* 입력 영역 카드 */
.st-key-add_box {
    padding: .75rem .9rem .5rem; border-radius: .9rem;
    background: rgba(22, 163, 74, .06); border: 1px solid rgba(22, 163, 74, .25);
}
.st-key-add_box button { white-space: nowrap; padding-left: .5rem !important; padding-right: .5rem !important; }

/* 행: 모바일에서도 컬럼이 세로로 쌓이지 않고 한 줄을 유지 */
.st-key-add_box [data-testid="stHorizontalBlock"],
[class*="st-key-row_"] [data-testid="stHorizontalBlock"],
.st-key-edit_box [data-testid="stHorizontalBlock"],
.st-key-undo_bar [data-testid="stHorizontalBlock"] {
    flex-wrap: nowrap !important; gap: .4rem !important;
}
.st-key-add_box [data-testid="stColumn"],
[class*="st-key-row_"] [data-testid="stColumn"],
.st-key-edit_box [data-testid="stColumn"],
.st-key-undo_bar [data-testid="stColumn"] {
    min-width: 0 !important;
}

/* 목록 행의 컬럼 폭: 체크박스·수량·버튼은 고정 폭, 품목명이 나머지를 차지 */
[class*="st-key-row_"] [data-testid="stColumn"]:nth-child(1) { flex: 0 0 2.2rem !important; width: 2.2rem !important; }
[class*="st-key-row_"] [data-testid="stColumn"]:nth-child(2) { flex: 1 1 auto !important; width: auto !important; }
[class*="st-key-row_"] [data-testid="stColumn"]:nth-child(3) { flex: 0 0 3.2rem !important; width: 3.2rem !important; }
[class*="st-key-row_"] [data-testid="stColumn"]:nth-child(4),
[class*="st-key-row_"] [data-testid="stColumn"]:nth-child(5) { flex: 0 0 2.75rem !important; width: 2.75rem !important; }
[class*="st-key-row_"] [data-testid="stHorizontalBlock"] { gap: .25rem !important; }

[class*="st-key-row_"] {
    padding: .15rem .25rem; border-bottom: 1px solid rgba(128, 128, 128, .18);
}
@media (hover: hover) {
    [class*="st-key-row_"]:hover { background: rgba(128, 128, 128, .05); border-radius: .5rem; }
}

/* Streamlit 마크다운 기본 음수 여백(-1rem) 때문에 행 높이가 줄고 세로 정렬이 어긋나는 문제 보정 */
[class*="st-key-row_"] [data-testid="stMarkdownContainer"] { margin-bottom: 0 !important; }
[class*="st-key-row_"] [data-testid="stMarkdownContainer"] p { margin: 0 !important; }

/* 체크박스: 누를 수 있는 영역을 약 44px로 넓힘 */
[class*="st-key-row_"] [data-testid="stCheckbox"] label {
    padding: .7rem .6rem; margin: -.7rem -.6rem; cursor: pointer;
}

/* 품목명 / 수량 */
.item-name { font-size: 1rem; overflow-wrap: anywhere; line-height: 1.4; padding-left: .15rem; }
.item-name.done { text-decoration: line-through; opacity: .45; }
.qty-badge {
    display: inline-block; min-width: 2.4rem; text-align: center; font-size: .85rem;
    padding: .1rem .45rem; border-radius: 999px; background: rgba(128, 128, 128, .14);
}
.qty-badge.done { opacity: .45; }

/* 아이콘 버튼: 손가락으로 누르기 쉽게 44px 정사각형 */
[class*="st-key-row_"] button {
    width: 2.75rem !important; min-height: 2.75rem !important; padding: 0 !important;
    font-size: 1.15rem; border-radius: .6rem !important;
}

/* 편집 중인 행 강조 */
.st-key-edit_box {
    padding: .6rem .75rem .75rem; border-radius: .75rem; margin: .25rem 0;
    background: rgba(245, 158, 11, .10); border: 1px solid rgba(245, 158, 11, .55);
}

/* 삭제 되돌리기 안내 */
.st-key-undo_bar {
    padding: .35rem .5rem .35rem .9rem; border-radius: .75rem; margin-bottom: .5rem;
    background: rgba(128, 128, 128, .12);
}
.undo-text { font-size: .9rem; overflow-wrap: anywhere; }
.st-key-undo_bar [data-testid="stMarkdownContainer"] { margin-bottom: 0 !important; }

/* 빈 목록 안내 */
.empty-box {
    text-align: center; padding: 2.2rem 1rem; border-radius: .9rem;
    border: 1px dashed rgba(128, 128, 128, .4); opacity: .8;
}
.empty-box .icon { font-size: 2.2rem; }

/* 좁은 화면 보정 */
@media (max-width: 640px) {
    .block-container { padding-left: 1rem; padding-right: 1rem; padding-top: 1.5rem; }
    .summary-card .num { font-size: 1.25rem; }
    /* 숫자 입력의 +/- 버튼을 숨겨 입력칸 폭 확보 */
    [data-testid="stNumberInput"] button { display: none; }
}
</style>
"""


def render_header() -> None:
    """제목과 요약 카드, 진행률 바."""
    st.title("🛒 쇼핑 리스트")
    items = st.session_state.shopping_items
    total = len(items)
    done = sum(1 for it in items if it["checked"])
    percent = round(done / total * 100) if total else 0
    st.markdown(
        f"""
        <div class="summary-card">
          <div class="stat"><span class="num">{total}</span><span class="label">전체</span></div>
          <div class="stat remain"><span class="num">{total - done}</span><span class="label">남은 항목</span></div>
          <div class="stat done"><span class="num">{done}</span><span class="label">완료</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.progress(done / total if total else 0.0, text=f"진행률 {percent}%")


def render_input() -> None:
    """아이템 입력 영역. form을 사용해 Enter로도 제출된다."""
    st.session_state.setdefault("add_name", "")
    st.session_state.setdefault("add_qty", MIN_QUANTITY)
    with st.container(key="add_box"):
        with st.form("add_form", border=False):
            col_name, col_qty, col_btn = st.columns([5, 1.7, 1.7], vertical_alignment="bottom")
            # max_chars를 쓰면 50자 초과 붙여넣기가 통째로 무시되므로, 길이는 validate()에서 안내한다.
            col_name.text_input("품목명", key="add_name", placeholder="예: 우유")
            col_qty.number_input("수량", key="add_qty", min_value=MIN_QUANTITY, max_value=MAX_QUANTITY, step=1)
            col_btn.form_submit_button("추가", on_click=handle_add, type="primary", use_container_width=True)
        st.caption(f"품목명은 {MAX_NAME_LENGTH}자까지, 수량은 {MIN_QUANTITY}~{MAX_QUANTITY} 사이 정수로 입력해요.")
    if st.session_state.add_error:
        st.warning(st.session_state.add_error, icon="⚠️")


def render_undo_bar() -> None:
    """삭제 직후 되돌리기 안내."""
    undo = st.session_state.undo
    if not undo:
        return
    with st.container(key="undo_bar"):
        col_msg, col_btn = st.columns([4, 1.6], vertical_alignment="center")
        col_msg.markdown(f'<div class="undo-text">🗑️ {html.escape(undo["message"])}</div>', unsafe_allow_html=True)
        col_btn.button("되돌리기", key="undo_btn", on_click=undo_delete, icon="↩️", use_container_width=True)


def render_item_row(item: dict) -> None:
    """일반 모드의 아이템 한 줄."""
    item_id = item["id"]
    done_cls = " done" if item["checked"] else ""
    # 다른 탭에서 체크 상태가 바뀌었을 수 있으므로 위젯 값을 파일 기준으로 맞춘다.
    st.session_state[f"chk_{item_id}"] = item["checked"]
    with st.container(key=f"row_{item_id}"):
        col_chk, col_name, col_qty, col_edit, col_del = st.columns(
            [0.55, 6, 1.3, 0.75, 0.75], vertical_alignment="center", gap="small"
        )
        col_chk.checkbox(
            item["name"],
            key=f"chk_{item_id}",
            on_change=toggle_item,
            args=(item_id,),
            label_visibility="collapsed",
        )
        # 사용자 입력은 반드시 html.escape 처리 후 출력
        col_name.markdown(
            f'<div class="item-name{done_cls}">{html.escape(item["name"])}</div>',
            unsafe_allow_html=True,
        )
        col_qty.markdown(
            f'<span class="qty-badge{done_cls}">×{int(item["quantity"])}</span>',
            unsafe_allow_html=True,
        )
        col_edit.button("✏️", key=f"edit_{item_id}", on_click=start_edit, args=(item_id,), help="수정", type="tertiary")
        col_del.button("🗑️", key=f"del_{item_id}", on_click=delete_item, args=(item_id,), help="삭제", type="tertiary")


def render_edit_row(item: dict) -> None:
    """편집 모드의 아이템 한 줄. form으로 감싸 Enter로도 저장된다."""
    item_id = item["id"]
    with st.container(key="edit_box"):
        with st.form(f"edit_form_{item_id}", border=False):
            col_name, col_qty = st.columns([5, 1.8], vertical_alignment="bottom")
            col_name.text_input("✏️ 품목명 수정", key=f"edit_name_{item_id}")
            col_qty.number_input("수량", key=f"edit_qty_{item_id}", min_value=MIN_QUANTITY, max_value=MAX_QUANTITY, step=1)
            col_save, col_cancel, _ = st.columns([1, 1, 2])
            # 첫 번째 제출 버튼(저장)이 Enter 키에 연결된다.
            col_save.form_submit_button(
                "저장", key=f"save_{item_id}", on_click=save_edit, args=(item_id,), type="primary", use_container_width=True
            )
            col_cancel.form_submit_button("취소", key=f"cancel_{item_id}", on_click=stop_edit, use_container_width=True)
        if st.session_state.edit_error:
            st.warning(st.session_state.edit_error, icon="⚠️")


def render_list() -> None:
    """아이템 목록 영역."""
    items = st.session_state.shopping_items
    render_undo_bar()

    # 편집 중이던 아이템이 다른 탭에서 지워졌으면 편집 모드 해제
    if st.session_state.editing_id and find_in(items, st.session_state.editing_id) is None:
        stop_edit()

    if not items:
        st.markdown(
            f'<div class="empty-box"><div class="icon">🧺</div><div>{EMPTY_MESSAGE}</div></div>',
            unsafe_allow_html=True,
        )
        return

    for item in items:
        if item["id"] == st.session_state.editing_id:
            render_edit_row(item)
        else:
            render_item_row(item)

    done_count = sum(1 for it in items if it["checked"])
    _, col_clear = st.columns([2, 1.3])
    col_clear.button(
        f"완료 항목 삭제 ({done_count})",
        key="clear_checked",
        on_click=clear_checked,
        disabled=done_count == 0,
        icon="🧹",
        use_container_width=True,
    )


def main() -> None:
    st.set_page_config(page_title="쇼핑 리스트", page_icon="🛒", layout="centered")
    st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
    init_state()
    refresh_items()

    if st.session_state.load_warning:
        st.warning(st.session_state.load_warning, icon="⚠️")
    if st.session_state.save_error:
        st.error(st.session_state.save_error, icon="🚨")

    render_header()
    render_input()
    st.write("")
    render_list()


if __name__ == "__main__":
    main()
