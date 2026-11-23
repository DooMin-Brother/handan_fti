import os
import re
import shutil
import gdown
import streamlit as st
from pypdf import PdfReader
from google import genai
from google.genai import types

# ==========================================
# 1. 기본 설정 및 Secrets 로드
# ==========================================
st.set_page_config(page_title="한단제어기술 FTI", page_icon="⚙️", layout="wide")

GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
GDRIVE_FOLDER_ID = st.secrets.get("GDRIVE_FOLDER_ID", "")
# 비워두면 전체 메이커 로드 / 예: ["미쓰비시"] 로 지정하면 해당 메이커 전용 앱으로 동작
TARGET_FOLDERS = st.secrets.get("TARGET_FOLDERS", [])
APP_TITLE = st.secrets.get("APP_TITLE", "한단제어기술 AI 기술지원 (FTI - 전체)")

CACHE_DIR = "./manuals_cache"

if not GEMINI_API_KEY or not GDRIVE_FOLDER_ID:
    st.error("Streamlit Secrets에 `GEMINI_API_KEY`와 `GDRIVE_FOLDER_ID`를 설정해 주세요.")
    st.stop()

client = genai.Client(api_key=GEMINI_API_KEY)


# ==========================================
# 2. 구글 드라이브 매뉴얼 다운로드 및 인덱싱
# ==========================================
@st.cache_resource(show_spinner="구글 드라이브에서 매뉴얼을 불러오고 있습니다...")
def load_manuals(folder_id: str, target_folders: list):
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR, exist_ok=True)
        url = f"https://drive.google.com/drive/folders/{folder_id}"
        gdown.download_folder(url=url, output=CACHE_DIR, quiet=True, use_cookies=False)

    # 다운로드된 하위 메이커 폴더 탐색
    makers = []
    documents = []  # {"maker", "file", "page", "text"}

    for item in sorted(os.listdir(CACHE_DIR)):
        item_path = os.path.join(CACHE_DIR, item)
        if not os.path.isdir(item_path):
            continue
        if target_folders and item not in target_folders:
            continue

        makers.append(item)
        for root, _, files in os.walk(item_path):
            for fname in sorted(files):
                if not fname.lower().endswith(".pdf"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    reader = PdfReader(fpath)
                    for idx, page in enumerate(reader.pages, start=1):
                        text = (page.extract_text() or "").strip()
                        if text:
                            documents.append({
                                "maker": item,
                                "file": fname,
                                "page": idx,
                                "text": text,
                            })
                except Exception:
                    continue

    return makers, documents


# ==========================================
# 3. 기술 용어/모델명 기반 관련 페이지 검색
# ==========================================
def search_manuals(query: str, documents: list, selected_maker: str, top_k: int = 8):
    # 영문/숫자 모델명, 에러코드, 한글 키워드 추출
    tokens = re.findall(r"[A-Za-z0-9\-_]+|[가-힣]{2,}", query.lower())
    if not tokens:
        return []

    scored = []
    for doc in documents:
        if selected_maker != "전체" and doc["maker"] != selected_maker:
            continue

        text_lower = doc["text"].lower()
        file_lower = doc["file"].lower()
        score = 0

        for tok in tokens:
            # 파일명에 키워드가 있으면 가중치 부여
            if tok in file_lower:
                score += 5
            # 본문 등장 횟수 (모델명/코드 등 영숫자 포함 시 가중치)
            cnt = text_lower.count(tok)
            if cnt > 0:
                weight = 3 if re.search(r"[a-z0-9]", tok) else 1
                score += min(cnt, 5) * weight

        if score > 0:
            scored.append((score, doc))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [doc for _, doc in scored[:top_k]]


# ==========================================
# 4. UI 구성
# ==========================================
st.title(f"⚙️ {APP_TITLE}")

makers, documents = load_manuals(GDRIVE_FOLDER_ID, TARGET_FOLDERS)

with st.sidebar:
    st.header("📂 매뉴얼 설정")
    if len(makers) > 1:
        selected_maker = st.selectbox("메이커 필터", ["전체"] + makers)
    elif len(makers) == 1:
        selected_maker = makers[0]
        st.info(f"전용 모드: **{selected_maker}**")
    else:
        selected_maker = "전체"
        st.warning("불러온 매뉴얼 폴더가 없습니다.")

    # 현재 로드된 파일 현황 표시
    loaded_files = sorted({(d["maker"], d["file"]) for d in documents})
    st.caption(f"로드된 매뉴얼: {len(loaded_files)}개 / 총 {len(documents):,}페이지")

    with st.expander("불러온 PDF 목록 보기"):
        for m, f in loaded_files:
            st.write(f"- [{m}] {f}")

    if st.button("🔄 드라이브 매뉴얼 새로고침"):
        if os.path.exists(CACHE_DIR):
            shutil.rmtree(CACHE_DIR)
        st.cache_resource.clear()
        st.rerun()

# 채팅 기록 초기화
if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# 사용자 질문 입력
if prompt := st.chat_input("에러 코드, 파라미터, 배선, 설정 방법 등을 질문하세요..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        with st.spinner("매뉴얼을 검색하고 답변을 작성 중입니다..."):
            hits = search_manuals(prompt, documents, selected_maker, top_k=8)

            context_blocks = []
            for h in hits:
                context_blocks.append(
                    f"[메이커: {h['maker']} | 파일: {h['file']} | {h['page']}p]\n{h['text'][:2500]}"
                )
            context_text = "\n\n---\n\n".join(context_blocks) if context_blocks else "관련 매뉴얼 발췌 없음"

            system_instruction = (
                "당신은 한단제어기술의 FA(공장자동화) 전문 엔지니어 AI(FTI)입니다. "
                "반드시 제공된 매뉴얼 발췌 내용을 최우선 근거로 답변하세요. "
                "파라미터 번호, 에러 코드, 배선/단자명, 사양 수치는 원문 그대로 정확히 표기하고, "
                "답변 끝에 참고한 [파일명 / 페이지]를 명시하세요. "
                "매뉴얼에 없는 내용은 추측하지 말고 '제공된 매뉴얼에서 확인되지 않습니다'라고 안내하세요."
            )

            full_prompt = f"### 참고 매뉴얼 발췌\n{context_text}\n\n### 사용자 질문\n{prompt}"

            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=full_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.2,
                ),
            )
            answer = response.text
            st.markdown(answer)

            if hits:
                with st.expander("📄 참고한 매뉴얼 원문 위치"):
                    for h in hits:
                        st.write(f"- **{h['maker']}** | `{h['file']}` ({h['page']}페이지)")

    st.session_state.messages.append({"role": "assistant", "content": answer})
