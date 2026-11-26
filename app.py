import os
import re
import csv
import io
import json
import base64
import asyncio
import urllib.request
import urllib.error
import fitz  # PyMuPDF
import streamlit as st
import streamlit.components.v1 as components

try:
    import edge_tts
    HAS_EDGE_TTS = True
except ImportError:
    HAS_EDGE_TTS = False

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
    HAS_GDRIVE = True
except ImportError:
    HAS_GDRIVE = False

st.set_page_config(page_title="한단제어기술 FTI", page_icon="⚙️", layout="wide")

st.markdown("""
<style>
div:has(> iframe[height="0"]) {
    display: none !important;
    height: 0 !important;
    margin: 0 !important;
    padding: 0 !important;
}
[data-testid="stAppViewContainer"], [data-testid="stMain"] {
    overflow-anchor: none !important;
}
[data-stale="true"] {
    opacity: 1 !important;
}
[data-testid="stMainBlockContainer"] {
    padding-bottom: 75vh !important;
}
</style>
""", unsafe_allow_html=True)

if "auth_user" not in st.session_state:
    _, center_col, _ = st.columns([1, 2, 1])
    ...
    st.stop()

# Streamlit Secrets에서 API 키 자동 로드 (없으면 사이드바에서 직접 입력)
default_api_key = st.secrets.get("GEMINI_API_KEY", "")
with st.sidebar:
    st.header("⚙️ 설정")
    api_key = st.text_input(
        "Gemini API 키",
        value=default_api_key,
        type="password"
    )

# 로컬 폴백 폴더 (Secrets가 없는 로컬 테스트용)
MANUALS_DIR = "Manuals" if os.path.exists("Manuals") else "manuals"

# ---------------------------------------------------------
# [0] 구글 드라이브 연동 함수 (Service Account)
# ---------------------------------------------------------
def has_drive_secrets():
    return (
        HAS_GDRIVE
        and "GCP_SERVICE_ACCOUNT_JSON" in st.secrets
        and "DRIVE_FOLDER_ID" in st.secrets
    )

@st.cache_resource
def get_drive_service():
    """서비스 계정 JSON으로 Google Drive API 클라이언트 생성"""
    sa_info = json.loads(st.secrets["GCP_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

@st.cache_data(ttl=600, show_spinner="구글 드라이브 매뉴얼 목록 불러오는 중...")
def get_drive_pdfs(root_folder_id):
    """구글 드라이브 폴더 및 하위 폴더의 모든 PDF를 {상대경로: 파일ID} 딕셔너리로 수집"""
    service = get_drive_service()
    pdf_map = {}

    def walk_folder(folder_id, prefix=""):
        page_token = None
        while True:
            resp = service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType)",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            for item in resp.get("files", []):
                name = item["name"]
                rel_path = f"{prefix}/{name}" if prefix else name
                if item["mimeType"] == "application/vnd.google-apps.folder":
                    walk_folder(item["id"], rel_path)
                elif name.lower().endswith(".pdf"):
                    pdf_map[rel_path] = item["id"]
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    walk_folder(root_folder_id)
    return dict(sorted(pdf_map.items()))

@st.cache_data(show_spinner=False, max_entries=30)
def download_drive_pdf(file_id):
    """구글 드라이브에서 특정 PDF 파일을 20MB 청크 단위로 고속 다운로드 및 캐싱"""
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request, chunksize=20 * 1024 * 1024)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return fh.getvalue()

def apply_fa_corrections(text):
    """음성인식(STT) FA 전문 용어 오인식 보정"""
    if not text:
        return text
    default_dict = {
        "서버 온": "서보온",
        "서버온": "서보온",
        "서버 드라이브": "서보 드라이브",
        "서보드라이버": "서보 드라이브",
        "미쯔비시": "미쓰비시",
        "이노벤스": "이노밴스",
        "렉스로쓰": "렉스로스",
        "스토바": "스토버",
        "피엘씨": "PLC",
        "지엑스 웍스": "GX Works",
    }
    personal_dict = st.session_state.get("personal_stt_corrections", {})
    combined_dict = {**default_dict, **personal_dict}
    for wrong, right in combined_dict.items():
        text = text.replace(wrong, right)
    return text

# ---------------------------------------------------------
# [1] AI 파일 라우터 + 스마트 PDF 슬라이싱 + Gemini REST API 호출 함수
# ---------------------------------------------------------
def get_local_pdfs(base_dir):
    """로컬 Manuals 폴더 폴백용"""
    pdf_map = {}
    if os.path.exists(base_dir):
        for root, _, files in os.walk(base_dir):
            for f in files:
                if f.lower().endswith(".pdf"):
                    rel_path = os.path.relpath(os.path.join(root, f), base_dir)
                    pdf_map[rel_path] = None
    return dict(sorted(pdf_map.items()))

STT_SHEET_ID = "1BL1hBiAauAa_oLnyRNsqMrOQpRGtmlqgzHq7ZiARTmY"

@st.cache_data(ttl=300)
def load_stt_dict(sheet_id):
    """구글 시트에서 음성인식 교정사전을 CSV로 불러옴 (5분마다 자동 갱신)"""
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as res:
            text = res.read().decode("utf-8-sig")
        stt_map = {}
        for row in csv.reader(io.StringIO(text)):
            if len(row) >= 2 and row[0].strip() and row[1].strip():
                if row[0].strip() == "오인식 단어":
                    continue
                stt_map[row[0].strip()] = row[1].strip()
        return stt_map
    except Exception as e:
        st.error(f"교정사전 로드 실패: {e}")
        return {}

def apply_stt_correction(prompt):
    """질문 문장에서 오인식 단어를 교정 단어로 자동 치환"""
    stt_map = load_stt_dict(STT_SHEET_ID)
    for wrong in sorted(stt_map.keys(), key=len, reverse=True):
        if wrong in prompt:
            prompt = prompt.replace(wrong, stt_map[wrong])
    st.caption(f"🔧 교정사전 {len(stt_map)}개 적용 → 변환된 질문: {prompt}")
    return prompt

def pick_pdfs_with_ai(prompt, all_pdfs, key):
    """전체 PDF 파일명 목록에서 질문에 가장 적합한 파일 1~2개만 AI가 0.5초 만에 선택"""
    if not all_pdfs:
        return []
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={key}"
    file_list_str = "\n".join([f"{i+1}. {p}" for i, p in enumerate(all_pdfs)])
    router_prompt = (
        f"다음은 보유 중인 FA 매뉴얼 PDF 목록입니다.\n{file_list_str}\n\n"
        f"사용자 질문: {prompt}\n\n"
        "위 질문에 답하기 위해 반드시 열어야 할 가장 관련 높은 매뉴얼 번호를 1~2개 골라 쉼표로 구분해 숫자만 출력하세요. (예: 12, 15)\n"
        "※ 미쓰비시 PLC 질문에서 기종이 명시되지 않은 경우, iQ-R(R 시리즈) → Q 시리즈 → FX 시리즈 우선순위로 매뉴얼을 선택하세요.\n"
        "※ 특정 PLC 명령어(예: IMASK, EI, DI, MOV, CJ, FROM, TO 등)의 기능·사용법·구조를 묻는 질문이면 반드시 '명령' 또는 '명령어'가 포함된 매뉴얼을 최우선으로 선택하세요. ('응용편'이나 '하드웨어편' 제외)"
    )
    data = {
        "contents": [{"parts": [{"text": router_prompt}]}],
        "generationConfig": {
            "temperature": 0.0,
            "thinkingConfig": {"thinkingBudget": 0}
        }
    }
    req = urllib.request.Request(
        url, data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            txt = json.loads(res.read().decode("utf-8"))["candidates"][0]["content"]["parts"][0]["text"]
            nums = [int(n) for n in re.findall(r'\d+', txt)]
            return [all_pdfs[n-1] for n in nums if 1 <= n <= len(all_pdfs)][:2]
    except Exception:
        return []

@st.cache_data(show_spinner=False, max_entries=30)
def get_pdf_pages_text(rel_path, file_id=None):
    """PDF 페이지별 순수 텍스트만 경량 추출해 메모리에 캐싱 (드라이브/로컬 겸용)"""
    pages = []
    if file_id:
        pdf_bytes = download_drive_pdf(file_id)
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    else:
        filepath = os.path.join(MANUALS_DIR, rel_path)
        doc = fitz.open(filepath)
    for pno in range(len(doc)):
        pages.append(doc[pno].get_text("text", flags=fitz.TEXT_PRESERVE_WHITESPACE) or "")
    doc.close()
    return pages

def extract_commands_from_image(img_bytes, mime_type, key):
    """래더 이미지에서 핵심 PLC 명령어(IMASK, EI 등)만 빠르게 추출"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={key}"
    payload = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(img_bytes).decode("utf-8")}},
                {"text": "이 PLC 래더 이미지에 사용된 주요 특수 명령어(예: IMASK, SIMASK, EI, DI, CJ, CALL, ZPUSH, FROM, TO 등)만 쉼표로 구분해 영문 대문자로 최대 5개만 출력하세요. 일반 접점(M, D, X, Y)이나 MOV, RST 같은 기초 명령은 제외하세요."}
            ]
        }]
    }
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.loads(res.read().decode("utf-8"))["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return ""

def extract_relevant_pdf_slice(prompt, target_pdfs, pdf_id_map, max_pages=6, stop_after_first_hit=True):
    """선별된 PDF 본문에서 목차를 제외하고 질문 키워드가 집중된 본문 페이지만 오려냄"""
    stop_words = {"대해", "대해서", "설명해", "설명해줘", "알려줘", "뭐야", "무엇", "어떻게", "프로그램에서", "설정", "방법", "있어", "해줘"}
    en_tokens = re.findall(r'[A-Za-z][A-Za-z0-9_]{1,}', prompt)
    ko_tokens = [w for w in re.findall(r'[가-힣]{2,}', prompt) if w not in stop_words]

    # 영문 FA 용어를 한글 매뉴얼 표기로 자동 확장
    en_to_ko_map = {
        "INDEX": ["인덱스", "인덱스 레지스터", "Z0"],
        "REGISTER": ["레지스터"],
        "INTERRUPT": ["인터럽트"],
        "TIMER": ["타이머"],
        "COUNTER": ["카운터"],
        "SERVO": ["서보"],
        "ALARM": ["알람"],
        "ERROR": ["에러", "오류"],
        "CAM": ["캠"],
        "ENCODER": ["엔코더"],
    }
    extra_tokens = []
    for tok in en_tokens:
        if tok.upper() in en_to_ko_map:
            extra_tokens.extend(en_to_ko_map[tok.upper()])

    keywords = list(dict.fromkeys(en_tokens + ko_tokens + extra_tokens))
    if not keywords:
        return None, []

    scored_pages = []
    for rel_path in target_pdfs:
        file_id = pdf_id_map.get(rel_path)
        if not file_id and not os.path.exists(os.path.join(MANUALS_DIR, rel_path)):
            continue
        file_scored = []
        try:
            pages_text = get_pdf_pages_text(rel_path, file_id=file_id)
            for pno, text in enumerate(pages_text):
                if not text:
                    continue
                text_upper = text.upper()

                dot_leaders = len(re.findall(r'[\.·…]{5,}', text))
                is_toc = (dot_leaders >= 3) or ("목차" in text[:200]) or ("CONTENTS" in text_upper[:200])

                score = 0
                for kw in keywords:
                    kw_up = kw.upper()
                    if kw.isascii():
                        cnt = len(re.findall(rf'\b{re.escape(kw_up)}\b', text_upper))
                        if cnt > 0:
                            score += min(cnt, 8) * 10
                            if re.search(rf'\b{re.escape(kw_up)}\b', text_upper[:350]):
                                score += 25
                    else:
                        cnt = text_upper.count(kw_up)
                        if cnt > 0:
                            score += min(cnt, 5) * 3

                if score > 0:
                    if any(m in text for m in ("기능", "처리 내용", "프로그램 예", "설정 데이터", "주의사항")):
                        score += 8
                    if is_toc:
                        score -= 40
                    if score > 0:
                        file_scored.append((score, rel_path, pno))
        except Exception:
            continue

        scored_pages.extend(file_scored)
        # 1순위 매뉴얼에서 충분한 본문 일치 페이지를 찾으면 2순위 PDF 다운로드 생략
        if stop_after_first_hit and file_scored and max(x[0] for x in file_scored) >= 20:
            break

    if not scored_pages:
        return None, []

    scored_pages.sort(key=lambda x: x[0], reverse=True)
    selected_map = {}
    total_added = 0

    for score, rel_path, pno in scored_pages:
        if total_added >= max_pages:
            break
        if rel_path not in selected_map:
            selected_map[rel_path] = set()
        for adj in (pno, pno + 1):
            if total_added < max_pages and adj not in selected_map[rel_path]:
                selected_map[rel_path].add(adj)
                total_added += 1

    out_doc = fitz.open()
    ref_info = []
    for rel_path, pnos in selected_map.items():
        file_id = pdf_id_map.get(rel_path)
        if file_id:
            src_doc = fitz.open(stream=download_drive_pdf(file_id), filetype="pdf")
        else:
            src_doc = fitz.open(os.path.join(MANUALS_DIR, rel_path))
        valid_pnos = sorted([p for p in pnos if 0 <= p < len(src_doc)])
        for p in valid_pnos:
            out_doc.insert_pdf(src_doc, from_page=p, to_page=p)
        src_doc.close()
        if valid_pnos:
            pages_str = ", ".join([f"{p+1}p" for p in valid_pnos])
            ref_info.append(f"{os.path.basename(rel_path)} ({pages_str})")

    if len(out_doc) == 0:
        out_doc.close()
        return None, []

    pdf_bytes = out_doc.tobytes()
    out_doc.close()
    return pdf_bytes, ref_info

# 구글 드라이브 우선 로드 (실패 시 로컬 폴더 폴백)
drive_mode = False
drive_error = None
if has_drive_secrets():
    try:
        pdf_id_map = get_drive_pdfs(st.secrets["DRIVE_FOLDER_ID"])
        drive_mode = True
    except Exception as e:
        drive_error = str(e)
        pdf_id_map = get_local_pdfs(MANUALS_DIR)
else:
    pdf_id_map = get_local_pdfs(MANUALS_DIR)

all_pdfs = list(pdf_id_map.keys())

# 좌측 사이드바: 비워두면 AI 자동 선택, 필요 시 특정 PDF만 수동 지정
with st.sidebar:
    st.markdown("---")
    source_badge = "☁️ Google Drive" if drive_mode else "💻 로컬"
    st.markdown(f"### 📚 원문 PDF 매뉴얼 ({len(all_pdfs)}개 인식 · {source_badge})")
    if drive_error:
        st.warning(f"드라이브 연동 오류: {drive_error}")
    if drive_mode and st.button("🔄 매뉴얼 목록 새로고침", use_container_width=True):
        get_drive_pdfs.clear()
        st.rerun()
    selected_pdfs = st.multiselect(
        "특정 매뉴얼만 검색 (비워두면 AI가 자동 선택)",
        options=all_pdfs,
        help="AI가 맞는 매뉴얼을 고른 뒤, 관련 페이지만 원문 그대로 오려내어 분석합니다."
    )

def ask_gemini_rest(prompt, key, uploaded_file=None, is_voice=False):
    import time

    if not key:
        return "⚠️ Gemini API 키가 설정되지 않았습니다. 좌측 사이드바 또는 Streamlit Secrets에 API 키를 입력해 주세요."
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={key}"
    headers = {"Content-Type": "application/json"}
    
    system_instruction = (
        "당신은 한단제어기술의 인공지능 FA 엔지니어(FTI)입니다. "
        "대화의 첫 번째 질문에 답할 때만 '한단제어기술 인공지능 엔지니어입니다.'로 간결하게 인사하고, "
        "이어지는 후속 질문에서는 인사말을 생략하고 바로 본론으로 답변하세요.\n"
        "질문 유형에 따라 답변 방식을 유연하게 선택하세요:\n"
        "1. [프로그램 전체 해석/진단 요청 시]: 코드를 한 줄씩 단순 중계하지 말고 아래 4단계로 핵심만 진단하세요.\n"
        "   - [핵심 포인트]: 이 프로그램의 핵심 동작이나 주의할 점을 1~2문장으로 요약\n"
        "   - [전체 목적 및 용도]: 어떤 설비나 상황에서 쓰는 구조인지 설명\n"
        "   - [진단]: 화면의 첫 스텝부터 마지막 스텝까지 빠짐없이 읽은 뒤 진단하세요. 정상적인 회로라면 억지로 결함을 만들어내지 말고 '정상 동작하는 회로입니다'라고 명확히 밝히세요.\n"
        "   - [제안]: 실제 수정이 필요한 부분이나 더 깔끔한 개선안 제시 (정상이라면 운영 시 참고사항만 간결히 안내)\n"
        "2. [특정 명령어 질문, 개념 설명, 후속 질문, 사실 정정 시]: 위 4단계 양식을 절대 쓰지 마세요. "
        "이미 설명한 내용을 반복하지 말고, 사용자가 물어본 핵심 질문에만 실무 엔지니어답게 직접적이고 명확하게 답변하세요. "
        "특히 PLC 명령어나 파라미터를 설명할 때는 아래 3가지를 반드시 포함하세요:\n"
        "   - 핵심 동작 기준 및 파라미터 정의 (비트 제어면 1/0 의미, 수치·위치결정·통신 명령이면 각 오퍼랜드의 역할·단위·설정 범위를 답변 초반에 명시)\n"
        "   - 구체적인 디바이스 할당 및 현장 래더 적용 예시 (명령어 실행 시 점유하는 워드/비트 수와 실제 래더 구성 예시)\n"
        "   - 선행 조건 및 현장 주의사항 (전원 투입 시 초기값, 함께 써야 하는 선행/짝 명령어, 디바이스 중복 충돌·스캔 타이밍·에러 발생 조건 등 실무 실수 포인트)\n"
        "※ [필수 주의사항]\n"
        "- 첨부된 원문 PDF 발췌 페이지가 있으면 일반 지식보다 PDF 원문(표·비트 매핑·래더 도표)을 최우선 근거로 삼아 정확히 답변하세요.\n"
        "- 화면이나 질문에서 확인된 CPU 기종의 디바이스 및 명령어 규칙만 적용하고, 타 기종의 특수 디바이스 규칙과 혼동하지 마세요.\n"
        "- 미쓰비시 PLC 질문에서 기종이 명시되지 않은 경우, 첨부된 원문 매뉴얼을 기준으로 iQ-R(R 시리즈) → Q 시리즈 → FX 시리즈 순서로 구분하여 설명하세요.\n"
        "- 래더의 병렬 분기선(세로선)으로 연결된 아래쪽 코일(SET/RST 등)을 조건 없는 모선 직결로 오해하지 마세요.\n"
        "- [래더 정밀 진단 원칙] 프로그램 이미지를 진단할 때는 반드시 다음을 먼저 검증하세요:\n"
        "  1) 미정의 접점 확인: 사용된 접점(예: M9000 등)을 구동하는 코일이 프로그램 내에 존재하는지 대조하세요.\n"
        "  2) 점프(CJ) 순서 확인: CJ 명령과 같은 블록의 아래쪽 병렬 출력(예: EI 등)은 CJ 실행 시 건너뛰어 미실행됨을 정확히 지적하세요.\n"
        "  3) 배타적 조건 및 가드 확인: 포인터(P10→P20) 아래로 흘러내리더라도 앞단 접점이 막고 있거나, 메인과 인터럽트 조건이 상호 배타적이면 오류로 오진하지 마세요.\n"
        "  4) 미쓰비시 고유 사양 준수: RST는 워드(D,W,R) 및 인덱스 레지스터(Z) 초기화에 정상 성립하며, iQ-R의 I48/I49는 고속 내부 타이머 인터럽트입니다."
    )

    if is_voice:
        system_instruction += (
            "\n[답변 분량 및 음성 안내 규칙]\n"
            "- 답변은 핵심을 놓치지 않으면서도 음성으로 듣기 편하도록 12~16줄(약 500~700자) 내외의 중간 분량으로 작성하세요.\n"
            "- 서론은 1문장으로 줄이고, 질문 유형에 맞춰 핵심 내용(사용법·명령어·설정 질문이면 동작 원리·파라미터·적용 예시, 알람·고장 질문이면 주요 원인·점검 및 조치 순서)을 항목당 1~2문장으로 명확히 설명하세요.\n"
            "- 예외 상황이나 아주 세부적인 내용은 생략하고, 끝에 '더 상세한 설명이나 추가 예시가 필요하면 말씀해 주세요.'라고 한 줄만 덧붙이세요.\n"
        )

    t0 = time.perf_counter()
    parts = []
    prompt = apply_stt_correction(prompt)
    has_en_keyword = bool(re.search(r'[A-Za-z]{2,}', prompt))
    t_pick = 0.0
    t_slice = 0.0

    if uploaded_file and not has_en_keyword and not selected_pdfs:
        slice_bytes, ref_info = None, []
    else:
        t1 = time.perf_counter()
        search_targets = list(selected_pdfs) if selected_pdfs else pick_pdfs_with_ai(prompt, all_pdfs, key)
        t_pick = time.perf_counter() - t1

        t2 = time.perf_counter()
        slice_bytes, ref_info = extract_relevant_pdf_slice(
            prompt, search_targets, pdf_id_map, max_pages=6, stop_after_first_hit=(not selected_pdfs)
        )
        t_slice = time.perf_counter() - t2

    if slice_bytes:
        parts.append({
            "inline_data": {
                "mime_type": "application/pdf",
                "data": base64.b64encode(slice_bytes).decode("utf-8")
            }
        })

    if uploaded_file:
        files_to_send = uploaded_file if isinstance(uploaded_file, list) else [uploaded_file]
        for f in files_to_send:
            file_bytes = f.getvalue()
            fname_lower = f.name.lower()
            if fname_lower.endswith((".csv", ".txt", ".xml", ".st")):
                # GX Works CSV(CP949/UTF-16) 및 일반 UTF-8 텍스트 자동 디코딩
                decoded_text = None
                for enc in ("utf-8-sig", "cp949", "utf-16", "euc-kr"):
                    try:
                        decoded_text = file_bytes.decode(enc)
                        break
                    except UnicodeDecodeError:
                        continue
                if decoded_text is None:
                    decoded_text = file_bytes.decode("utf-8", errors="replace")
                parts.append({"text": f"### 첨부된 PLC 프로그램 파일 ({f.name}):\n```\n{decoded_text}\n```"})
            else:
                b64_data = base64.b64encode(file_bytes).decode("utf-8")
                mime_type = "application/pdf" if fname_lower.endswith(".pdf") else (f.type or "application/octet-stream")
                parts.append({
                    "inline_data": {
                        "mime_type": mime_type,
                        "data": b64_data
                    }
                })

    parts.append({"text": f"{system_instruction}\n\n### 질문:\n{prompt}"})

    data = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.2,
            "thinkingConfig": {
                "thinkingBudget": 1024 if uploaded_file is not None else 0
            }
        }
    }
    
    req = urllib.request.Request(
        url, 
        data=json.dumps(data).encode("utf-8"), 
        headers=headers, 
        method="POST"
    )
    
    try:
        t3 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=120) as response:
            res_body = response.read().decode("utf-8")
            res_json = json.loads(res_body)
            answer = res_json["candidates"][0]["content"]["parts"][0]["text"]
            answer = answer.replace("**", "")
            t_api = time.perf_counter() - t3
            t_total = time.perf_counter() - t0

            ref_tag = (
                f"\n\n---\n📚 **원문 PDF 발췌 참조:** {' / '.join(ref_info)}"
                if ref_info
                else "\n\n---\n⚠️ **매뉴얼 내 일치 페이지 없음 (일반 지식 답변)**"
            )
            time_tag = f"\n\n⏱️ `파일선택 {t_pick:.1f}초 | 발췌 {t_slice:.1f}초 | AI응답 {t_api:.1f}초 (합계 {t_total:.1f}초)`"
            return answer + ref_tag + time_tag
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8")
        return f"❌ API 호출 오류 (HTTP {e.code}): {err_msg}"
    except Exception as e:
        return f"❌ 통신 중 오류가 발생했습니다: {str(e)}"

# ---------------------------------------------------------
# [2] 백쿼트(`) 키 핸즈프리 음성 제어 (좌측 사이드바 고정 + 알림음)
# ---------------------------------------------------------
with st.sidebar:
    st.markdown("---")
    st.markdown("### 🎙️ 현장 음성 제어")

    stt_html = """
    <div style="background-color: #1e1e1e; color: #d4d4d4; padding: 10px; border-radius: 8px; font-family: 'Malgun Gothic', monospace; border: 1px solid #333;">
        <div id="status-banner" style="font-weight: bold; font-size: 13px; color: #ff6b6b; margin-bottom: 8px; line-height: 1.4;">
            🔴 대기 중 (`키 1회: 시작 / 2회: 전송)
        </div>
        <div id="live-transcript" style="min-height: 48px; max-height: 85px; overflow-y: auto; background-color: #2d2d2d; padding: 8px; border-radius: 6px; color: #ffffff; font-size: 13px; font-weight: bold; line-height: 1.4;">
            대기 중...
        </div>
    </div>

    <script>
    const statusBanner = document.getElementById('status-banner');
    const liveTranscript = document.getElementById('live-transcript');
    const parentDoc = window.parent.document;

    let recognizing = false;
    let recognition = null;
    let accumulatedText = '';

    function playBeep(type) {
        try {
            const AudioCtx = window.AudioContext || window.webkitAudioContext;
            const ctx = new AudioCtx();
            const now = ctx.currentTime;

            if (type === 'start') {
                const osc1 = ctx.createOscillator();
                const gain1 = ctx.createGain();
                osc1.type = 'sine';
                osc1.frequency.setValueAtTime(880, now);
                osc1.frequency.setValueAtTime(1320, now + 0.08);
                gain1.gain.setValueAtTime(0.2, now);
                gain1.gain.exponentialRampToValueAtTime(0.001, now + 0.22);
                osc1.connect(gain1);
                gain1.connect(ctx.destination);
                osc1.start(now);
                osc1.stop(now + 0.22);
            } else {
                const osc2 = ctx.createOscillator();
                const gain2 = ctx.createGain();
                osc2.type = 'sine';
                osc2.frequency.setValueAtTime(420, now);
                osc2.frequency.setValueAtTime(300, now + 0.08);
                gain2.gain.setValueAtTime(0.2, now);
                gain2.gain.exponentialRampToValueAtTime(0.001, now + 0.20);
                osc2.connect(gain2);
                gain2.connect(ctx.destination);
                osc2.start(now);
                osc2.stop(now + 0.20);
            }
        } catch (e) {}
    }

    if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        recognition = new SpeechRecognition();
        recognition.lang = 'ko-KR';
        recognition.interimResults = true;
        recognition.continuous = true;

        recognition.onstart = function() {
            recognizing = true;
            accumulatedText = '';
            liveTranscript.innerText = '말씀해 주세요...';
            statusBanner.style.color = '#51cf66';
            statusBanner.innerText = "🟢 음성모드 시작됨 (`키로 전송)";
        };

        recognition.onresult = function(event) {
            let interimTranscript = '';
            let finalTranscript = '';
            for (let i = 0; i < event.results.length; ++i) {
                if (event.results[i].isFinal) {
                    finalTranscript += event.results[i][0].transcript + ' ';
                } else {
                    interimTranscript += event.results[i][0].transcript;
                }
            }
            accumulatedText = (finalTranscript + interimTranscript).trim();
            liveTranscript.innerText = accumulatedText || '말씀해 주세요...';
        };

        recognition.onerror = function(event) {
            console.error("Speech recognition error", event.error);
        };

        recognition.onend = function() {
            recognizing = false;
            statusBanner.style.color = '#ff6b6b';
            statusBanner.innerText = "🔴 대기 중 (`키 1회: 시작 / 2회: 전송)";

            if (window._pendingVoiceSubmit) {
                window._pendingVoiceSubmit = false;
                const finalQuery = (accumulatedText || liveTranscript.innerText || "").trim();
                liveTranscript.innerText = "전송 중: " + finalQuery;
                submitToStreamlitChat(finalQuery);
            }
        };
    } else {
        statusBanner.innerText = "⚠️ Chrome 또는 Edge 브라우저를 권장합니다.";
    }

    function submitToStreamlitChat(text) {
        if (!text || text === '대기 중...' || text === '말씀해 주세요...') return;
        
        const chatInput = parentDoc.querySelector('textarea[data-testid="stChatInputTextArea"]');
        if (chatInput) {
            chatInput.focus();
            const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.parent.HTMLTextAreaElement.prototype, "value").set;
            nativeInputValueSetter.call(chatInput, "[VOICE] " + text);
            chatInput.dispatchEvent(new window.parent.Event('input', { bubbles: true }));
            
            setTimeout(() => {
                const submitBtn = parentDoc.querySelector('button[data-testid="stChatInputSubmitButton"]');
                if (submitBtn && !submitBtn.disabled) {
                    submitBtn.click();
                } else {
                    chatInput.dispatchEvent(new window.parent.KeyboardEvent('keydown', {
                        key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true
                    }));
                }
            }, 250);
        }
    }

    let isVoiceActive = false;

    function handleBackquote(event) {
        if (event.code === 'Backquote' || event.key === '`') {
            event.preventDefault();
            event.stopPropagation();
            if (event.repeat) return;

            if (!isVoiceActive) {
                isVoiceActive = true;
                playBeep('start');
                if ('speechSynthesis' in window.parent) {
                    window.parent.speechSynthesis.cancel();
                }
                if (recognition) {
                    try { recognition.start(); } catch(e) {}
                }
            } else {
                isVoiceActive = false;
                playBeep('end');
                window._pendingVoiceSubmit = true;
                if (recognition) {
                    try { recognition.stop(); } catch(e) {}
                }
            }
        }
    }

    if (window.parent._ftiBackquoteHandler) {
        parentDoc.removeEventListener('keydown', window.parent._ftiBackquoteHandler, true);
    }
    window.parent._ftiBackquoteHandler = handleBackquote;

    document.addEventListener('keydown', handleBackquote, true);
    parentDoc.addEventListener('keydown', handleBackquote, true);
    </script>
    """
    components.html(stt_html, height=130)

# ---------------------------------------------------------
# [3] 이전 대화 기록 화면 출력
# ---------------------------------------------------------
if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# ---------------------------------------------------------
# [4] 질문 처리, FTI 대답 생성 및 1.2배속 TTS 출력
# ---------------------------------------------------------
def speak_browser_tts(text):
    """긴 답변도 지연 없이 즉시 말하도록 문장 단위로 쪼개어 속도·음량 적용 출력"""
    tts_lines = [line for line in text.splitlines() if not line.strip().startswith(("📚", "⚠️", "---"))]
    text = "\n".join(tts_lines)

    sentences = [s.strip() for s in re.split(r'[\n.!?]+', text) if s.strip()]
    safe_sentences = json.dumps(sentences)
    tts_js = f"""
    <script>
    if ('speechSynthesis' in window.parent) {{
        window.parent.speechSynthesis.cancel();
        const sentences = {safe_sentences};
        sentences.forEach((sentence) => {{
            const utterance = new SpeechSynthesisUtterance(sentence);
            utterance.lang = 'ko-KR';
            utterance.rate = 1.2;
            utterance.volume = 1.0;
            window.parent.speechSynthesis.speak(utterance);
        }});
    }}
    </script>
    """
    components.html(tts_js, height=0)

with st.sidebar:
    st.markdown("### 🖥️ PLC 툴 화면 공유")
    screen_share_html = """
    <div style="display:flex; flex-direction:column; gap:8px; font-family:sans-serif;">
        <button id="btnShare" style="padding:8px; background:#2563eb; color:white; border:none; border-radius:6px; cursor:pointer; font-weight:bold;">
            🖥️ PLC 툴 화면 공유 시작
        </button>
        <button id="btnCapture" disabled style="padding:8px; background:#10b981; color:white; border:none; border-radius:6px; cursor:pointer; font-weight:bold; opacity:0.5;">
            📸 공유 화면 채팅창에 첨부 (F2)
        </button>
        <div id="shareStatus" style="font-size:12px; color:#555;">상태: 공유 꺼짐</div>
    </div>

    <script>
    const pWin = window.parent;
    const pDoc = pWin.document;
    const btnShare = document.getElementById('btnShare');
    const btnCapture = document.getElementById('btnCapture');
    const statusEl = document.getElementById('shareStatus');

    function updateUI() {
        const isSharing = pWin._ftiStream && pWin._ftiStream.active;
        if (isSharing) {
            btnShare.textContent = "⏹️ 화면 공유 중지";
            btnShare.style.background = "#dc2626";
            btnCapture.disabled = false;
            btnCapture.style.opacity = "1";
            statusEl.textContent = "상태: 🟢 PLC 툴 공유 중 (F2로 즉시 첨부)";
        } else {
            btnShare.textContent = "🖥️ PLC 툴 화면 공유 시작";
            btnShare.style.background = "#2563eb";
            btnCapture.disabled = true;
            btnCapture.style.opacity = "0.5";
            statusEl.textContent = "상태: 공유 꺼짐";
        }
    }

    btnShare.onclick = async () => {
        if (pWin._ftiStream && pWin._ftiStream.active) {
            pWin._ftiStream.getTracks().forEach(t => t.stop());
            pWin._ftiStream = null;
            pWin._ftiVideo = null;
            updateUI();
            return;
        }
        try {
            const stream = await pWin.navigator.mediaDevices.getDisplayMedia({
                video: { cursor: "always" },
                audio: false
            });
            const video = pDoc.createElement('video');
            video.srcObject = stream;
            video.muted = true;
            await video.play();

            pWin._ftiStream = stream;
            pWin._ftiVideo = video;

            stream.getVideoTracks()[0].onended = () => {
                pWin._ftiStream = null;
                pWin._ftiVideo = null;
                updateUI();
            };
            updateUI();
        } catch (err) {
            statusEl.textContent = "공유가 취소되었습니다.";
        }
    };

    function captureToChat() {
        if (!pWin._ftiVideo || !pWin._ftiStream || !pWin._ftiStream.active) {
            statusEl.textContent = "⚠️ 먼저 '화면 공유 시작'을 눌러주세요.";
            return;
        }

        const video = pWin._ftiVideo;
        const canvas = pDoc.createElement('canvas');
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);

        const dataUrl = canvas.toDataURL('image/jpeg', 0.85);
        const bstr = atob(dataUrl.split(',')[1]);
        const u8arr = new Uint8Array(bstr.length);
        for (let i = 0; i < bstr.length; i++) u8arr[i] = bstr.charCodeAt(i);

        const file = new File([u8arr], `plc_screen_${Date.now()}.jpg`, { type: 'image/jpeg' });
        const dropzone = pDoc.querySelector('[data-testid="stFileUploaderDropzone"]');
        const fileInput = pDoc.querySelector('[data-testid="stFileUploaderDropzoneInput"]');
        const timeStr = new Date().toLocaleTimeString('ko-KR');

        if (dropzone) {
            const dt = new DataTransfer();
            dt.items.add(file);
            const dropEvent = new DragEvent('drop', {
                bubbles: true,
                cancelable: true,
                dataTransfer: dt
            });
            dropzone.dispatchEvent(dropEvent);
            statusEl.textContent = `✅ 누적 첨부 완료 (${timeStr})`;
        } else if (fileInput) {
            const dt = new DataTransfer();
            if (fileInput.files) {
                for (let i = 0; i < fileInput.files.length; i++) {
                    dt.items.add(fileInput.files[i]);
                }
            }
            dt.items.add(file);
            fileInput.files = dt.files;
            fileInput.dispatchEvent(new Event('change', { bubbles: true }));
            statusEl.textContent = `✅ 누적 첨부 완료 (${timeStr})`;
        } else {
            statusEl.textContent = "⚠️ 파일 업로더를 찾지 못했습니다.";
        }
    }

    btnCapture.onclick = captureToChat;

    const handleF2 = (e) => {
        if (e.key === 'F2' && pWin._ftiStream && pWin._ftiStream.active) {
            e.preventDefault();
            captureToChat();
        }
    };
    if (pWin._ftiCaptureKeyHandler) {
        pDoc.removeEventListener('keydown', pWin._ftiCaptureKeyHandler, true);
    }
    pWin._ftiCaptureKeyHandler = handleF2;
    pDoc.addEventListener('keydown', handleF2, true);
    document.addEventListener('keydown', handleF2, true);

    updateUI();
    </script>
    """
    components.html(screen_share_html, height=130)
    st.markdown("---")

    st.markdown("### 📎 현장 미디어 첨부")
    uploaded_file = st.file_uploader(
        "사진·동영상·소리·PLC 프로그램(PDF/CSV/ST/TXT) 파일을 올린 뒤 질문하세요.",
        type=["png", "jpg", "jpeg", "webp", "mp4", "mov", "webm", "mp3", "wav", "m4a", "pdf", "csv", "txt", "xml", "st"],
        accept_multiple_files=True,
        help="화면 캡처는 Win+Shift+S 후 드래그하거나, GX Works CSV/PDF 파일을 올려주세요. (여러 장 누적 첨부 가능, 최대 20MB 권장)"
    )

    if uploaded_file:
        valid_files = []
        for f in uploaded_file:
            file_mb = f.size / (1024 * 1024)
            if file_mb > 20:
                st.error(f"'{f.name}' 크기({file_mb:.1f}MB)가 20MB를 초과해 제외되었습니다.")
            else:
                valid_files.append(f)
                st.caption(f"📎 {f.name} ({file_mb:.1f}MB)")
        uploaded_file = valid_files

raw_prompt = st.chat_input(
    "질문을 입력하거나 파일(PDF/CSV/ST/이미지) 드래그 / 캡처 붙여넣기(Ctrl+V) / 음성(`) 질문하세요...",
    accept_file="multiple",
    file_type=["png", "jpg", "jpeg", "webp", "mp4", "mov", "webm", "mp3", "wav", "m4a", "pdf", "csv", "txt", "xml", "st"]
)

def scroll_to_message(msg_index):
    """실제 스크롤 영역을 자동 탐색해 방금 한 질문을 화면 최상단에 고정"""
    import time
    nonce = time.time_ns()
    components.html(f"""
    <script>
    // nonce: {nonce}
    (function() {{
        let tries = 0;
        let userScrolled = false;
        const pWin = window.parent;
        const doc = pWin.document;

        const stopScroll = () => {{ userScrolled = true; }};
        pWin.addEventListener('wheel', stopScroll, {{ once: true, passive: true, capture: true }});
        pWin.addEventListener('touchstart', stopScroll, {{ once: true, passive: true, capture: true }});

        function getScrollContainer(el) {{
            let cur = el.parentElement;
            while (cur && cur !== doc.body) {{
                const ov = pWin.getComputedStyle(cur).overflowY;
                if ((ov === 'auto' || ov === 'scroll') && cur.scrollHeight > cur.clientHeight + 10) {{
                    return cur;
                }}
                cur = cur.parentElement;
            }}
            return doc.scrollingElement || doc.documentElement;
        }}

        const timer = setInterval(() => {{
            tries++;
            if (userScrolled || tries > 15) {{
                clearInterval(timer);
                return;
            }}
            const messages = doc.querySelectorAll('[data-testid="stChatMessage"]');
            const target = messages[{msg_index}];
            if (target) {{
                const scroller = getScrollContainer(target);
                const sRect = scroller.getBoundingClientRect ? scroller.getBoundingClientRect() : {{ top: 0 }};
                const tRect = target.getBoundingClientRect();
                const topPos = scroller.scrollTop + (tRect.top - sRect.top) - 80;
                scroller.scrollTop = Math.max(0, topPos);
            }}
        }}, 100);
    }})();
    </script>
    """, height=0)

def render_file_badge(file_obj):
    """첨부 파일 미리보기(이미지 썸네일 / 문서 카드)"""
    if not file_obj:
        return
    fname = file_obj.name
    fsize_kb = len(file_obj.getvalue()) / 1024
    is_img = fname.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))

    if is_img:
        st.image(file_obj.getvalue(), width=220, caption=f"🖼️ {fname} ({fsize_kb:.1f}KB)")
    else:
        st.markdown(
            f"""
            <div style="display:inline-flex; align-items:center; gap:8px; background:#f0f4f9; border:1px solid #d2e3fc; border-radius:10px; padding:8px 12px; margin-bottom:8px; font-size:13px; color:#1f1f1f;">
                <span style="font-size:18px;">📄</span>
                <div>
                    <div style="font-weight:bold;">{fname}</div>
                    <div style="font-size:11px; color:#5f6368;">첨부됨 · {fsize_kb:.1f}KB</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True
        )

if raw_prompt:
    user_input = raw_prompt.text
    active_file = list(raw_prompt.files) if raw_prompt.files else (list(uploaded_file) if uploaded_file else [])

    if not user_input and active_file:
        user_input = "이 첨부 파일을 분석하고 문제점이나 핵심 내용을 설명해 줘."

    is_voice_query = False
    if user_input.startswith("[VOICE]"):
        is_voice_query = True
        user_input = user_input.replace("[VOICE]", "", 1).strip()

    corrected_input = apply_fa_corrections(user_input)

    file_names = ", ".join(f"`{f.name}`" for f in active_file)
    file_label = f"📎 **첨부 파일 ({len(active_file)}개):** {file_names}\n\n" if active_file else ""
    st.session_state.messages.append({"role": "user", "content": file_label + corrected_input})
    user_msg_idx = len(st.session_state.messages) - 1

    components.html("""
    <script>
    const p = window.parent;
    if (p._ftiAudio) {
        p._ftiAudio.pause();
        p._ftiAudio.currentTime = 0;
    }
    if (p._ftiQueue) {
        p._ftiQueue = [];
    }
    if (p.speechSynthesis) {
        p.speechSynthesis.cancel();
    }
    </script>
    """, height=0)

    with st.chat_message("user"):
        if active_file:
            for f in active_file:
                render_file_badge(f)
        st.markdown(corrected_input)
    scroll_to_message(user_msg_idx)

    spinner_msg = f"📎 첨부 파일 {len(active_file)}개를 함께 분석하고 있습니다..." if active_file else "FTI가 대답을 찾고 있습니다..."
    with st.chat_message("assistant"):
        with st.spinner(spinner_msg):
            answer_text = ask_gemini_rest(corrected_input, api_key, uploaded_file=active_file, is_voice=is_voice_query)

        st.markdown(answer_text)
        st.session_state.messages.append({"role": "assistant", "content": answer_text})
        scroll_to_message(user_msg_idx)

        if is_voice_query:
            tts_slot = st.empty()
            clean_tts_text = answer_text.replace("*", "").replace("#", "").replace("`", "").strip()

            sentences = [s.strip() for s in re.split(r'(?<=[.!?\n])\s+', clean_tts_text) if s.strip()]
            chunks, current = [], ""
            for s in sentences:
                limit = 80 if not chunks else 250
                if len(current) + len(s) <= limit:
                    current = (current + " " + s).strip()
                else:
                    if current:
                        chunks.append(current)
                    current = s
            if current:
                chunks.append(current)

            components.html("""
            <script>
            if (window.parent._ftiCurrentAudio) {
                window.parent._ftiCurrentAudio.pause();
                window.parent._ftiCurrentAudio = null;
            }
            window.parent._ftiAudioQueue = [];
            window.parent._ftiIsPlaying = false;
            window.parent._ftiPaused = false;

            window.parent._ftiToggleAudio = function() {
                if (!window.parent._ftiPaused) {
                    window.parent._ftiPaused = true;
                    if (window.parent._ftiCurrentAudio) {
                        window.parent._ftiCurrentAudio.pause();
                    }
                    if (window.parent.speechSynthesis && window.parent.speechSynthesis.speaking) {
                        window.parent.speechSynthesis.pause();
                    }
                } else {
                    window.parent._ftiPaused = false;
                    if (window.parent._ftiCurrentAudio && window.parent._ftiCurrentAudio.paused) {
                        window.parent._ftiCurrentAudio.play().catch(function() {
                            window.parent._ftiCurrentAudio = null;
                            window.parent._ftiIsPlaying = false;
                            window.parent._ftiPlayNext();
                        });
                    } else {
                        window.parent._ftiIsPlaying = false;
                        window.parent._ftiPlayNext();
                    }
                    if (window.parent.speechSynthesis && window.parent.speechSynthesis.paused) {
                        window.parent.speechSynthesis.resume();
                    }
                }
            };

            window.parent._ftiPlayNext = function() {
                if (window.parent._ftiPaused || window.parent._ftiIsPlaying || window.parent._ftiAudioQueue.length === 0) return;
                window.parent._ftiIsPlaying = true;
                const src = window.parent._ftiAudioQueue.shift();
                const audio = new window.parent.Audio(src);
                audio.playbackRate = 1.0;
                audio.volume = 1.0;
                window.parent._ftiCurrentAudio = audio;
                audio.onended = function() {
                    window.parent._ftiCurrentAudio = null;
                    window.parent._ftiIsPlaying = false;
                    window.parent._ftiPlayNext();
                };
                audio.onerror = function() {
                    window.parent._ftiCurrentAudio = null;
                    window.parent._ftiIsPlaying = false;
                    window.parent._ftiPlayNext();
                };
                audio.play().catch(function() {
                    window.parent._ftiCurrentAudio = null;
                    window.parent._ftiIsPlaying = false;
                });
            };

            if (window.parent._ftiEscHandler) {
                window.parent.removeEventListener("keydown", window.parent._ftiEscHandler, true);
            }
            window.parent._ftiEscHandler = function(e) {
                if (e.key === "Escape" && !e.repeat) {
                    e.preventDefault();
                    e.stopPropagation();
                    window.parent._ftiToggleAudio();
                }
            };
            window.parent.addEventListener("keydown", window.parent._ftiEscHandler, true);
            </script>
            """, height=0)

            tts_played = False
            st.caption(f"🔍 TTS 진단: HAS_EDGE_TTS={HAS_EDGE_TTS}")
            if HAS_EDGE_TTS:
                async def generate_tts_bytes(text):
                    text = re.split(r'(?:\n\s*---\s*\n)?\s*(?:📚\s*\*{0,2}원문 PDF 발췌 참조|⚠️\s*\*{0,2}매뉴얼 내 일치 페이지 없음|⏱️)', text)[0]
                    text = text.replace("**", "").replace("`", "")
                    text = re.sub(r'([A-Za-z0-9가-힣._+()-]+)\s*[~～∼〜˜⁓∽]\s*([A-Za-z0-9가-힣._+()-]+)', r'\1에서 \2까지', text)
                    text = re.sub(r'[~～∼〜˜⁓∽]', '에서 ', text).replace("→", "에서 ")
                    text = re.sub(r'\+[-–—―─━－]+|[-–—―─━－=_*]{2,}|//+', ' ', text)
                    text = re.sub(r'[|│├└┌┐┘┤┬┴┼\[\]]', ' ', text)
                    text = re.sub(r'(?<!\S)\+(?!\S)', ' ', text)
                    text = text.replace(";", " ").replace(":", " ")
                    text = re.sub(r'[ \t]{2,}', ' ', text).strip()
                    if not text:
                        return b""

                    communicate = edge_tts.Communicate(text, "ko-KR-SunHiNeural", rate="+20%", volume="+20%")
                    audio_data = b""
                    async for chunk in communicate.stream():
                        if chunk["type"] == "audio":
                            audio_data += chunk["data"]
                    return audio_data

                try:
                    for chunk_text in chunks:
                        audio_bytes = asyncio.run(generate_tts_bytes(chunk_text))
                        if audio_bytes:
                            b64_audio = base64.b64encode(audio_bytes).decode("utf-8")
                            with tts_slot:
                                components.html(f"""
                                <script>
                                window.parent._ftiAudioQueue.push("data:audio/mp3;base64,{b64_audio}");
                                window.parent._ftiPlayNext();
                                </script>
                                """, height=0)
                            tts_played = True
                except Exception as e:
                    st.caption(f"⚠️ edge-tts 실패(브라우저 음성 대체): {e}")
                    tts_played = False

            if not tts_played:
                fallback_text = re.split(r'(?:\n\s*---\s*\n)?\s*(?:📚\s*\*{0,2}원문 PDF 발췌 참조|⚠️\s*\*{0,2}매뉴얼 내 일치 페이지 없음|⏱️)', clean_tts_text)[0]
                fallback_text = fallback_text.replace("**", "").replace("`", "")
                fallback_text = re.sub(r'([A-Za-z0-9가-힣._+()-]+)\s*[~～∼〜˜⁓∽]\s*([A-Za-z0-9가-힣._+()-]+)', r'\1에서 \2까지', fallback_text)
                fallback_text = re.sub(r'[~～∼〜˜⁓∽]', '에서 ', fallback_text).replace("→", "에서 ")
                fallback_text = re.sub(r'\+[-–—―─━－]+|[-–—―─━－=_*]{2,}|//+', ' ', fallback_text)
                fallback_text = re.sub(r'[|│├└┌┐┘┤┬┴┼\[\]]', ' ', fallback_text)
                fallback_text = re.sub(r'(?<!\S)\+(?!\S)', ' ', fallback_text)
                fallback_text = fallback_text.replace(";", " ").replace(":", " ")
                fallback_text = re.sub(r'[ \t]{2,}', ' ', fallback_text).strip()
                speak_browser_tts(fallback_text)