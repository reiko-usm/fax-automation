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
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="FAX発注書管理",
    page_icon="📠",
    layout="wide"
)

SPREADSHEET_NAME = "FAX発注書管理"
HEADERS = ["FAX日付", "商品名", "数量", "発注者", "納品先名称", "納品先郵便番号", "納品先住所", "納品先電話番号", "納品希望日", "発注№", "お客様№", "備考", "ファイル名"]

DATA_KEYS = {
    "商品名": "商品名",
    "数量": "数量",
    "発注者": "発注者",
    "納品先": "納品先",
    "納品希望日": "納品希望日",
    "発注№": "発注番号",
    "お客様№": "顧客番号",
    "備考": "備考",
}


def get_anthropic_client():
    api_key = st.secrets.get("ANTHROPIC_API_KEY", os.getenv("ANTHROPIC_API_KEY"))
    if not api_key:
        st.error("ANTHROPIC_API_KEY が設定されていません")
        st.stop()
    return anthropic.Anthropic(api_key=api_key)


def get_sheets_client():
    b64 = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_B64", os.getenv("GOOGLE_SERVICE_ACCOUNT_B64", ""))
    if not b64:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_B64 が設定されていません")
    service_account_info = json.loads(base64.b64decode(b64).decode("utf-8"))
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(service_account_info, scopes=scope)
    return gspread.authorize(creds)


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

FAX日付について：用紙の右端・左端・ヘッダー部分にある送信日時、作成日、発注日、NSPHFAX等のFAXシステムが記録した日付を読み取ってください。

{
  "fax日付": "FAX用紙に記載の日付（YYYY年M月D日形式、不明は空）",
  "商品名": "発注商品名（複数ある場合はカンマ区切り）",
  "数量": "数量と単位",
  "発注者": "発注者名または会社名",
  "納品先名称": "納品先の会社名・店舗名など（住所は除く）",
  "納品先郵便番号": "納品先の郵便番号（ハイフンあり、例：123-4567）",
  "納品先住所": "納品先の住所（都道府県から番地まで、建物名含む）",
  "納品先電話番号": "納品先の電話番号",
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
    """Google Sheetsの納品月タブにデータを追記する（ヘッダー名で列を検索）。"""
    try:
        gc = get_sheets_client()
        spreadsheet_id = st.secrets.get("SPREADSHEET_ID", os.getenv("SPREADSHEET_ID", ""))
        if spreadsheet_id:
            spreadsheet = gc.open_by_key(spreadsheet_id)
        else:
            try:
                spreadsheet = gc.open(SPREADSHEET_NAME)
            except gspread.SpreadsheetNotFound:
                return False, "スプレッドシートが見つかりません"

        tab_name = get_month_tab_name(data.get("納品希望日", ""))

        try:
            worksheet = spreadsheet.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=len(HEADERS))
            worksheet.append_row(HEADERS)

        header_row = worksheet.row_values(1)
        if not header_row:
            worksheet.append_row(HEADERS)
            header_row = HEADERS

        # ヘッダー名で列を検索して書き込む（列の追加・入れ替えに対応）
        row_data = {
            "FAX日付": data.get("FAX日付", ""),
            "商品名": data.get("商品名", ""),
            "数量": data.get("数量", ""),
            "発注者": data.get("発注者", ""),
            "納品先名称": data.get("納品先名称", ""),
            "納品先郵便番号": data.get("納品先郵便番号", ""),
            "納品先住所": data.get("納品先住所", ""),
            "納品先電話番号": data.get("納品先電話番号", ""),
            "納品希望日": data.get("納品希望日", ""),
            "発注№": data.get("発注番号", ""),
            "お客様№": data.get("顧客番号", ""),
            "備考": data.get("備考", ""),
            "ファイル名": filename,
        }
        row = [row_data.get(h, "") for h in header_row]
        worksheet.append_row(row)
        return True, spreadsheet.url

    except Exception as e:
        import traceback
        return False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"


# ── UI ──────────────────────────────────────────────────────────────────────
st.title("📠 FAX発注書管理")
st.caption("FAX画像をアップロードして、発注書データをGoogle Sheetsに自動保存します")

uploaded_files = st.file_uploader(
    "FAX画像・PDFをアップロード（複数可）",
    type=["jpg", "jpeg", "png", "tif", "tiff", "pdf"],
    accept_multiple_files=True,
    help="PCFaxで受信した画像ファイルをここにドロップしてください",
)

if uploaded_files:
    st.info(f"{len(uploaded_files)} ファイル選択済み")

    if st.button("🔍 AIで一括読み取り", type="primary", use_container_width=True):
        results = []
        progress = st.progress(0)
        for i, f in enumerate(uploaded_files):
            with st.spinner(f"読み取り中: {f.name}（{i+1}/{len(uploaded_files)}）"):
                try:
                    f.seek(0)
                    image_bytes, media_type = process_file(f)
                    extracted = extract_fax_data(image_bytes, media_type)
                    results.append({
                        "ファイル名": f.name,
                        "FAX日付": extracted.get("fax日付", ""),
                        "商品名": extracted.get("商品名", ""),
                        "数量": extracted.get("数量", ""),
                        "発注者": extracted.get("発注者", ""),
                        "納品先名称": extracted.get("納品先名称", ""),
                        "納品先郵便番号": extracted.get("納品先郵便番号", ""),
                        "納品先住所": extracted.get("納品先住所", ""),
                        "納品先電話番号": extracted.get("納品先電話番号", ""),
                        "納品希望日": extracted.get("納品希望日", ""),
                        "発注№": extracted.get("発注番号", ""),
                        "お客様№": extracted.get("顧客番号", ""),
                        "備考": extracted.get("備考", ""),
                    })
                except Exception as e:
                    st.error(f"❌ {f.name}: {e}")
            progress.progress((i + 1) / len(uploaded_files))
        st.session_state["results"] = results
        st.success(f"読み取り完了！{len(results)} 件")

if "results" in st.session_state and st.session_state["results"]:
    st.divider()
    st.subheader("📋 内容の確認・修正")
    st.caption("セルをクリックして直接編集できます。修正後に「全て保存」してください。")

    edited_df = st.data_editor(
        pd.DataFrame(st.session_state["results"]),
        use_container_width=True,
        hide_index=True,
        num_rows="fixed",
    )

    if st.button("✅ 全て保存", type="primary", use_container_width=True):
        success_count = 0
        for _, row in edited_df.iterrows():
            filename = row.get("ファイル名", "")
            data = {
                "FAX日付": row.get("FAX日付", ""),
                "商品名": row.get("商品名", ""),
                "数量": row.get("数量", ""),
                "発注者": row.get("発注者", ""),
                "納品先名称": row.get("納品先名称", ""),
                "納品先郵便番号": row.get("納品先郵便番号", ""),
                "納品先住所": row.get("納品先住所", ""),
                "納品先電話番号": row.get("納品先電話番号", ""),
                "納品希望日": row.get("納品希望日", ""),
                "発注番号": row.get("発注№", ""),
                "顧客番号": row.get("お客様№", ""),
                "備考": row.get("備考", ""),
            }
            success, result = save_to_sheets(data, filename)
            if success:
                success_count += 1
                st.success(f"✅ {filename}")
            else:
                st.error(f"❌ {filename}: {result}")

        if success_count > 0:
            st.balloons()
            st.markdown(f"[📊 Google Sheetsを開く]({result})")
            del st.session_state["results"]
