import streamlit as st
import anthropic
import gspread
from google.oauth2.service_account import Credentials
import json
import base64
import io
from datetime import datetime
import re
from PIL import Image
import fitz  # PyMuPDF
import os
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="FAX発注書管理",
    page_icon="📠",
    layout="centered"
)

SPREADSHEET_NAME = "FAX発注書管理"
HEADERS = ["受信日", "商品名", "数量", "発注者", "納品先", "納品希望日", "発注№", "お客様№", "備考", "ファイル名"]


def get_anthropic_client():
    api_key = st.secrets.get("ANTHROPIC_API_KEY", os.getenv("ANTHROPIC_API_KEY"))
    if not api_key:
        st.error("ANTHROPIC_API_KEY が設定されていません")
        st.stop()
    return anthropic.Anthropic(api_key=api_key)


def get_sheets_client():
    try:
        service_account_json = st.secrets.get("GOOGLE_SERVICE_ACCOUNT", os.getenv("GOOGLE_SERVICE_ACCOUNT"))
        if not service_account_json:
            return None
        service_account_info = json.loads(service_account_json)
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(service_account_info, scopes=scope)
        return gspread.authorize(creds)
    except Exception:
        return None


def process_file(uploaded_file):
    """アップロードされたファイルをPNG画像バイトに変換する。"""
    file_bytes = uploaded_file.read()
    filename = uploaded_file.name.lower()

    if filename.endswith(".pdf"):
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        page = doc[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        return pix.tobytes("png"), "image/png"
    elif filename.endswith((".tif", ".tiff")):
        img = Image.open(io.BytesIO(file_bytes))
        img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), "image/png"
    elif filename.endswith((".jpg", ".jpeg")):
        return file_bytes, "image/jpeg"
    else:
        return file_bytes, "image/png"


def extract_fax_data(image_bytes, media_type):
    """Claude Vision APIで発注書の項目を抽出する。"""
    client = get_anthropic_client()

    prompt = """この発注書FAXから以下の項目を読み取り、JSON形式のみで返してください（説明文不要）。
項目が記載されていない場合は空文字列にしてください。

{
  "商品名": "発注商品名（複数ある場合はカンマ区切り）",
  "数量": "数量と単位",
  "発注者": "発注者名または会社名",
  "納品先": "納品先の名称・住所",
  "納品希望日": "希望納品日（YYYY年M月D日形式、不明は空）",
  "発注番号": "発注番号・注文番号",
  "顧客番号": "お客様番号・顧客番号",
  "備考": "その他特記事項"
}"""

    message = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.standard_b64encode(image_bytes).decode("utf-8"),
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )

    response_text = message.content[0].text.strip()
    if "```json" in response_text:
        response_text = response_text.split("```json")[1].split("```")[0].strip()
    elif "```" in response_text:
        response_text = response_text.split("```")[1].split("```")[0].strip()

    return json.loads(response_text)


def get_month_tab_name(delivery_date_str):
    """納品希望日から保存先タブ名を決定する。"""
    if delivery_date_str:
        match = re.search(r"(\d{4})年(\d{1,2})月", delivery_date_str)
        if match:
            return f"{match.group(1)}年{int(match.group(2))}月"
    now = datetime.now()
    return f"{now.year}年{now.month}月"


def save_to_sheets(data, filename):
    """Google Sheetsの納品月タブにデータを追記する。"""
    gc = get_sheets_client()
    if not gc:
        return False, "Google Sheetsの認証情報が設定されていません"

    try:
        spreadsheet_id = st.secrets.get("SPREADSHEET_ID", os.getenv("SPREADSHEET_ID", ""))
        if spreadsheet_id:
            spreadsheet = gc.open_by_key(spreadsheet_id)
        else:
            try:
                spreadsheet = gc.open(SPREADSHEET_NAME)
            except gspread.SpreadsheetNotFound:
                return False, "スプレッドシートが見つかりません。SPREADSHEET_IDを設定してください。"

        tab_name = get_month_tab_name(data.get("納品希望日", ""))

        try:
            worksheet = spreadsheet.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=len(HEADERS))
            worksheet.append_row(HEADERS)

        if not worksheet.row_values(1):
            worksheet.append_row(HEADERS)

        row = [
            datetime.now().strftime("%Y/%m/%d %H:%M"),
            data.get("商品名", ""),
            data.get("数量", ""),
            data.get("発注者", ""),
            data.get("納品先", ""),
            data.get("納品希望日", ""),
            data.get("発注番号", ""),
            data.get("顧客番号", ""),
            data.get("備考", ""),
            filename,
        ]
        worksheet.append_row(row)
        return True, spreadsheet.url

    except Exception as e:
        import traceback
        return False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"


# ── UI ──────────────────────────────────────────────────────────────────────
st.title("📠 FAX発注書管理")
st.caption("FAX画像をアップロードして、発注書データをGoogle Sheetsに自動保存します")

uploaded_file = st.file_uploader(
    "FAX画像・PDFをアップロード",
    type=["jpg", "jpeg", "png", "tif", "tiff", "pdf"],
    help="PCFaxで受信した画像ファイルをここにドロップしてください",
)

if uploaded_file:
    col1, col2 = st.columns([1, 1])

    with col1:
        st.subheader("FAX画像")
        if uploaded_file.name.lower().endswith(".pdf"):
            st.info(f"📄 PDFファイル: {uploaded_file.name}")
        else:
            uploaded_file.seek(0)
            st.image(uploaded_file, use_column_width=True)

    with col2:
        st.subheader("自動読み取り")
        if st.button("🔍 AIで読み取る", type="primary", use_container_width=True):
            with st.spinner("Claude AIが読み取り中...（数秒かかります）"):
                try:
                    uploaded_file.seek(0)
                    image_bytes, media_type = process_file(uploaded_file)
                    extracted = extract_fax_data(image_bytes, media_type)
                    st.session_state["extracted"] = extracted
                    st.session_state["filename"] = uploaded_file.name
                    st.success("読み取り完了！下で内容を確認・修正してください。")
                except Exception as e:
                    st.error(f"読み取りエラー: {e}")

if "extracted" in st.session_state:
    st.divider()
    st.subheader("📋 内容の確認・修正")
    st.caption("AIが読み取った内容です。誤りがあれば修正してから保存してください。")

    data = st.session_state["extracted"]

    col1, col2 = st.columns(2)
    with col1:
        data["商品名"] = st.text_input("商品名", value=data.get("商品名", ""))
        data["数量"] = st.text_input("数量", value=data.get("数量", ""))
        data["発注者"] = st.text_input("発注者", value=data.get("発注者", ""))
        data["納品先"] = st.text_input("納品先", value=data.get("納品先", ""))
    with col2:
        data["納品希望日"] = st.text_input("納品希望日", value=data.get("納品希望日", ""))
        data["発注番号"] = st.text_input("発注№", value=data.get("発注番号", ""))
        data["顧客番号"] = st.text_input("お客様№", value=data.get("顧客番号", ""))
        data["備考"] = st.text_input("備考", value=data.get("備考", ""))

    tab_name = get_month_tab_name(data.get("納品希望日", ""))
    st.info(f"💾 保存先タブ: **{tab_name}**（納品希望日から自動判定）")

    if st.button("✅ Google Sheetsに保存", type="primary", use_container_width=True):
        with st.spinner("保存中..."):
            success, result = save_to_sheets(data, st.session_state.get("filename", ""))
            if success:
                st.success("保存しました！")
                st.markdown(f"[📊 Google Sheetsを開く]({result})")
                del st.session_state["extracted"]
            else:
                st.error(f"保存エラー: {result}")
