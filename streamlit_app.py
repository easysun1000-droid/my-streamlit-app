from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from typing import Any

import streamlit as st


API_URL = (
    "https://apis.data.go.kr/1160100/"
    "GetStockSecuritiesInfoService_V2/getStockPriceInfo_V2"
)


class ApiError(Exception):
    """조회 실패 사유를 사용자에게 안전하게 표시하기 위한 예외."""


def api_error_message(code: str, message: str) -> str:
    hints = {
        "20": "이 API 서비스의 활용신청·승인 상태를 확인하세요.",
        "22": "일일 요청 한도를 초과했습니다.",
        "30": (
            "공공데이터포털의 ‘마이페이지 → 인증키 발급현황’에서 이 서비스에 "
            "사용할 일반 인증키를 다시 복사하세요."
        ),
        "31": "인증키 사용 기한이 만료되었습니다.",
    }
    return f"API 오류 {code}: {message} {hints.get(code, '')}".strip()


def parse_error_response(raw: bytes, http_status: int) -> str:
    text = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return f"API 서버가 HTTP {http_status} 오류를 반환했습니다. {text[:250]}"

    service_error = payload.get("OpenAPI_ServiceResponse", {}).get("cmmMsgHeader", {})
    if service_error:
        code = str(service_error.get("returnReasonCode", ""))
        message = str(
            service_error.get("returnAuthMsg")
            or service_error.get("errMsg")
            or "인증 오류"
        )
        return api_error_message(code, message)

    header = payload.get("response", {}).get("header", {})
    if header:
        return api_error_message(
            str(header.get("resultCode", "")),
            str(header.get("resultMsg", "인증 오류")),
        )
    return f"API 서버가 HTTP {http_status} 오류를 반환했습니다. {text[:250]}"


def number(value: Any, kind: type[int] | type[float]) -> int | float:
    try:
        return kind(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return kind(0)


def normalize_service_key(api_key: str, key_format: str) -> str:
    key = api_key.strip()
    if not key:
        raise ApiError("API 인증키를 입력해 주세요.")
    if key_format not in {"auto", "encoding", "decoding"}:
        raise ApiError("인증키 형식 값이 올바르지 않습니다.")

    if key_format == "encoding" or (key_format == "auto" and "%" in key):
        key = urllib.parse.unquote(key)
    return urllib.parse.quote(key, safe="")


def fetch_stocks(
    api_key: str,
    target_date: str,
    key_format: str = "auto",
) -> dict[str, Any]:
    try:
        parsed_date = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ApiError("날짜 형식이 올바르지 않습니다.") from exc

    if parsed_date > date.today():
        raise ApiError("오늘 이후 날짜는 조회할 수 없습니다.")

    query = urllib.parse.urlencode(
        {
            "numOfRows": "10000",
            "pageNo": "1",
            "resultType": "json",
            "basDt": parsed_date.strftime("%Y%m%d"),
        }
    )
    encoded_key = normalize_service_key(api_key, key_format)
    request = urllib.request.Request(
        f"{API_URL}?serviceKey={encoded_key}&{query}",
        headers={"User-Agent": "KoreanStockDashboard/1.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise ApiError(parse_error_response(exc.read(), exc.code)) from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"API 서버에 연결할 수 없습니다: {exc.reason}") from exc

    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        preview = raw.decode("utf-8", errors="replace")[:250]
        raise ApiError(
            "API 응답을 읽을 수 없습니다. 인증키와 서비스 활용신청 상태를 "
            f"확인하세요. ({preview})"
        ) from exc

    api_response = payload.get("response", {})
    header = api_response.get("header", {})
    result_code = str(header.get("resultCode", ""))
    result_message = str(header.get("resultMsg", "응답 메시지 없음"))
    if result_code not in {"00", "0"}:
        raise ApiError(api_error_message(result_code, result_message))

    body = api_response.get("body", {})
    items_node = body.get("items") or {}
    raw_items = items_node.get("item", []) if isinstance(items_node, dict) else []
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    if not raw_items:
        raise ApiError(
            "선택한 날짜의 데이터가 없습니다. 휴장일이거나 아직 데이터가 "
            "공개되지 않았을 수 있습니다."
        )

    stocks = [
        {
            "name": str(item.get("itmsNm", "")),
            "code": str(item.get("srtnCd", "")),
            "price": number(item.get("clpr"), int),
            "change": number(item.get("vs"), int),
            "rate": number(item.get("fltRt"), float),
        }
        for item in raw_items
    ]
    stocks.sort(key=lambda item: (-item["price"], item["name"]))
    return {
        "date": parsed_date.isoformat(),
        "total": int(number(body.get("totalCount", len(stocks)), int)),
        "items": stocks[:50],
        "source_message": result_message,
    }


st.set_page_config(page_title="한국 주식 종가 조회", page_icon="📈", layout="wide")

st.title("한국 주식 종가 조회")
st.caption("선택한 거래일의 종목을 현재가가 높은 순서로 정렬해 상위 50개를 보여줍니다.")

with st.form("stock_query"):
    key_column, format_column, date_column, button_column = st.columns(
        [4, 1.3, 1.5, 1]
    )
    with key_column:
        api_key = st.text_input(
            "공공데이터포털 API 인증키",
            type="password",
            autocomplete="off",
            help="인증키는 저장하지 않고 조회 요청에만 사용합니다.",
        )
    with format_column:
        key_format_label = st.selectbox(
            "인증키 형식",
            ["자동 감지", "Encoding", "Decoding"],
        )
    with date_column:
        target_date = st.date_input(
            "기준 날짜",
            value=date.today(),
            max_value=date.today(),
        )
    with button_column:
        st.write("")
        submitted = st.form_submit_button(
            "조회하기",
            use_container_width=True,
            type="primary",
        )

st.info(
    "이 API는 일 1회 갱신되는 종가 자료입니다. 장중 실시간 가격이나 과거 "
    "특정 시각의 가격은 제공하지 않습니다.",
    icon="ℹ️",
)

if submitted:
    if not api_key.strip():
        st.error("API 인증키를 입력해 주세요.")
    else:
        key_formats = {
            "자동 감지": "auto",
            "Encoding": "encoding",
            "Decoding": "decoding",
        }
        with st.spinner("공공데이터 API에서 주식 데이터를 조회하고 있습니다…"):
            try:
                result = fetch_stocks(
                    api_key,
                    target_date.isoformat(),
                    key_formats[key_format_label],
                )
            except ApiError as error:
                st.error(str(error))
            except Exception:
                st.error("예상하지 못한 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")
            else:
                st.success(f"정상 조회되었습니다. ({result['source_message']})")
                st.caption(
                    f"기준일 {result['date']} · 전체 {result['total']:,}개 중 "
                    f"상위 {len(result['items'])}개"
                )
                rows = [
                    {
                        "순위": index,
                        "종목명": item["name"],
                        "종목코드": item["code"],
                        "현재가": f"{item['price']:,}원",
                        "전일 대비": (
                            f"{item['change']:+,}원" if item["change"] else "0원"
                        ),
                        "등락률": (
                            f"{item['rate']:+.2f}%" if item["rate"] else "0.00%"
                        ),
                    }
                    for index, item in enumerate(result["items"], start=1)
                ]
                st.dataframe(
                    rows,
                    hide_index=True,
                    use_container_width=True,
                    height=900,
                )
