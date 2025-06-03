# -*- coding: utf-8 -*-
# 필요한 라이브러리 임포트
import feedparser # RSS 피드 파싱
import requests # HTTP 요청
from bs4 import BeautifulSoup # HTML 파싱
import markdownify # HTML을 Markdown으로 변환
import os # 환경 변수 및 파일 시스템 접근
from datetime import datetime, timedelta, timezone # 시간 처리
import pytz # 시간대 처리
import re # 정규 표현식 (슬러그 생성 등)
import logging # 로깅
from dotenv import load_dotenv # 로컬 환경 변수 로드용

# 로깅 기본 설정
# 로그 레벨: INFO, WARNING, ERROR, DEBUG, CRITICAL
# 로그 포맷: 시간 - 로그레벨 - 메시지
# 로그 출력: 콘솔 (stdout)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 로컬 개발 환경일 경우 .env 파일에서 환경 변수 로드
# GitHub Actions 환경에서는 .env 파일 없이 Secrets를 통해 환경 변수 주입
if os.path.exists('.env'):
    load_dotenv()

# --- 설정 변수 ---
# 사용자가 이 부분을 자신의 필요에 맞게 수정할 수 있습니다.

# 뉴스 소스 RSS 피드 URL 목록
# 각 항목은 딕셔너리 형태로, 'name' (뉴스 소스 이름)과 'url' (RSS 피드 주소)을 가집니다.
NEWS_SOURCES = [
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "Cointelegraph", "url": "https://cointelegraph.com/rss"},
    {"name": "Decrypt", "url": "https://decrypt.co/feed"},
    {"name": "Bitcoin.com News", "url": "https://news.bitcoin.com/feed/"},
    {"name": "BeInCrypto", "url": "https://beincrypto.com/feed/"}
    # 필요에 따라 다른 뉴스 소스를 추가할 수 있습니다.
]

# 생성된 Markdown 파일이 저장될 디렉토리
OUTPUT_DIR = "_posts"

# 각 뉴스 소스에서 가져올 최대 기사 수 (너무 많으면 처리 시간 증가)
MAX_ARTICLES_PER_SOURCE = 10

# 최종적으로 선정할 상위 기사 수
NUM_TOP_ARTICLES = 3

# 최근 몇 시간 내의 기사를 수집할지 (예: 24시간)
HOURS_AGO = 24

# 기사 본문 요약이 이 길이보다 짧으면 전체 내용 가져오기 시도 (문자 수 기준)
SUMMARY_MIN_LENGTH = 250 

# 한국 시간대 (KST)
KST = pytz.timezone('Asia/Seoul')

# --- 유틸리티 함수 ---

def slugify(text):
    """
    텍스트를 URL 및 파일명에 적합한 슬러그(slug)로 변환합니다.
    예: "Hello World! 123" -> "hello-world-123"
    """
    text = text.lower() # 소문자로 변환
    text = re.sub(r'\s+', '-', text) # 공백을 하이픈으로 대체
    text = re.sub(r'[^\w\-]+', '', text) # 영숫자, 밑줄, 하이픈 이외의 문자 제거
    text = re.sub(r'\-\-+', '-', text) # 연속된 하이픈을 단일 하이픈으로
    text = text.strip('-') # 양 끝의 하이픈 제거
    return text[:70] # 슬러그 길이 제한 (70자)

def get_article_published_date(entry):
    """
    feedparser entry에서 발행일 정보를 추출하여 timezone-aware datetime 객체로 반환합니다.
    RSS 피드마다 날짜 필드 이름이나 형식이 다를 수 있어 여러 필드를 확인합니다.
    기본적으로 UTC로 가정합니다.
    """
    date_fields = ['published_parsed', 'updated_parsed']
    parsed_time = None
    for field in date_fields:
        if hasattr(entry, field) and entry[field]:
            parsed_time = entry[field]
            break
    
    if parsed_time:
        try:
            # time.struct_time을 datetime 객체로 변환 (UTC 기준)
            return datetime(*parsed_time[:6], tzinfo=timezone.utc)
        except Exception as e:
            logging.warning(f"날짜 변환 중 오류 ({entry.get('link', 'N/A')}): {e}")
    return None

def fetch_full_content_from_url(article_url):
    """
    주어진 URL에서 기사 본문을 HTML로 가져와 Markdown으로 변환 시도합니다.
    RSS 피드의 요약 내용이 충분하지 않을 경우 사용됩니다.
    """
    try:
        headers = { # 일부 웹사이트는 User-Agent를 요구함
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(article_url, timeout=20, headers=headers)
        response.raise_for_status() # HTTP 오류 발생 시 예외 발생
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # 주요 기사 내용이 포함될 가능성이 높은 HTML 태그/클래스 선택자 목록
        # 순서대로 시도하며, 사이트 구조에 따라 조정이 필요할 수 있습니다.
        content_selectors = [
            'article.article-content', 'div.article-content', 'div.post-content', 
            'div.entry-content', 'section.article__body', 'div.main-content',
            'article', 'main' 
        ]
        
        article_html_content = None
        for selector in content_selectors:
            element = soup.select_one(selector)
            if element:
                # 불필요한 요소 (스크립트, 스타일, 광고, 댓글 등) 제거 시도
                for el_to_remove in element.select('script, style, nav, footer, aside, .ad, .advertisement, .related-articles, .comments-area'):
                    el_to_remove.decompose()
                article_html_content = str(element)
                break
        
        if not article_html_content and soup.body: # 위 선택자로 못 찾으면 body 전체를 사용 (최후의 수단)
            logging.warning(f"특정 콘텐츠 영역을 찾지 못해 body 전체를 사용합니다: {article_url}")
            article_html_content = str(soup.body)

        if article_html_content:
            # HTML을 Markdown으로 변환
            # heading_style='ATX' (# H1, ## H2 등)
            # bullets='*' (리스트 항목에 * 사용)
            markdown_text = markdownify.markdownify(article_html_content, heading_style='ATX', bullets='*').strip()
            # 변환 후 너무 짧으면 유의미한 내용이 없다고 판단 가능
            if len(markdown_text) < SUMMARY_MIN_LENGTH:
                 logging.warning(f"추출된 전체 내용이 너무 짧습니다 ({len(markdown_text)}자): {article_url}")
                 return None # 혹은 원본 요약을 그대로 사용하도록 None 반환
            return markdown_text
        else:
            logging.warning(f"HTML에서 내용을 추출하지 못했습니다: {article_url}")
            return None

    except requests.RequestException as e:
        logging.error(f"전체 기사 내용 요청 실패 ({article_url}): {e}")
        return None
    except Exception as e:
        logging.error(f"HTML 파싱 또는 Markdown 변환 중 오류 ({article_url}): {e}")
        return None

# --- 번역 및 요약 함수 (플레이스홀더) ---
# TODO: 사용자는 이 함수를 실제 번역/요약 API와 연동해야 합니다.
# 예를 들어, OpenRouter (무료 모델 포함), DeepL API, Google Cloud Translation API 등을 사용할 수 있습니다.
# API 키는 환경 변수 (예: TRANSLATION_API_KEY)를 통해 안전하게 관리하세요.
# 이 함수는 영어 제목과 영어 본문을 입력받아, 한국어 제목과 한국어 요약 본문을 반환해야 합니다.
def translate_and_summarize_content(english_title, english_content, target_language="ko"):
    """
    실제 번역 및 요약 API로 대체되어야 하는 플레이스홀더 함수입니다.
    현재는 원문 텍스트와 함께 안내 메시지를 포함한 플레이스홀더 값을 반환합니다.
    """
    api_key = os.getenv("TRANSLATION_API_KEY") 

    if not api_key:
        logging.warning(
            "TRANSLATION_API_KEY 환경 변수가 설정되지 않았습니다. "
            "실제 번역 및 요약을 위해서는 유효한 API 키를 설정해야 합니다. "
            "현재는 원본 내용 기반의 플레이스홀더가 사용됩니다."
        )
        korean_title_placeholder = f"[미번역] {english_title}"
        korean_summary_placeholder = (
            f"[번역 및 요약되지 않음 - TRANSLATION_API_KEY 필요]\n\n"
            f"이곳에 '{english_title}' 기사의 한국어 번역 및 요약 내용이 와야 합니다.\n"
            f"현재는 원본 영어 내용의 일부만 표시됩니다:\n\n{english_content[:800]}...\n\n"
            f"---\n**안내:** `TRANSLATION_API_KEY` 환경 변수를 설정하고, "
            f"`translate_and_summarize_content` 함수 내에 실제 번역/요약 API 호출 로직을 구현해주세요. "
            f"OpenRouter API (예: 'deepseek/deepseek-chat-v3-0324:free' 모델) 등을 활용할 수 있습니다."
        )
        return korean_title_placeholder, korean_summary_placeholder

    logging.info(f"'{english_title}'의 번역 및 요약을 시도합니다 (현재 플레이스홀더 동작). API 키 일부: {api_key[:4]}...")
    
    # --- 사용자가 구현할 실제 API 연동 로직 ---
    # 아래는 예시일 뿐이며, 사용자는 자신이 선택한 API 서비스의 문서를 참조하여 구현해야 합니다.
    # 예시: OpenRouter API (deepseek/deepseek-chat-v3-0324:free 모델 사용)
    # try:
    #     headers = {
    #         "Authorization": f"Bearer {api_key}",
    #         "Content-Type": "application/json",
    #         "HTTP-Referer": "YOUR_SITE_URL", # OpenRouter 권장 사항
    #         "X-Title": "YOUR_PROJECT_TITLE" # OpenRouter 권장 사항
    #     }
    #
    #     # 1. 제목 번역
    #     title_payload = {
    #         "model": "deepseek/deepseek-chat-v3-0324:free",
    #         "messages": [
    #             {"role": "system", "content": f"You are an expert translator. Translate the following English news headline into concise and natural {target_language}."},
    #             {"role": "user", "content": english_title}
    #         ]
    #     }
    #     title_response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=title_payload, timeout=30)
    #     title_response.raise_for_status()
    #     korean_title = title_response.json()['choices'][0]['message']['content'].strip()
    #
    #     # 2. 내용 번역 및 요약
    #     # (요청 길이 제한에 유의. 필요시 내용 분할 또는 요약 후 번역)
    #     content_summary_prompt = (
    #         f"Please perform the following two tasks on the English news article content provided below:\n"
    #         f"1. Translate the entire article into natural {target_language}.\n"
    #         f"2. After translating, summarize the {target_language} content into 3-5 main paragraphs, focusing on key information and insights.\n"
    #         f"Ensure the final output is only the {target_language} summary.\n\n"
    #         f"English Article Content:\n{english_content[:3000]}" # API 토큰 제한 고려하여 내용 자르기
    #     )
    #     summary_payload = {
    #         "model": "deepseek/deepseek-chat-v3-0324:free", # 또는 더 큰 컨텍스트를 가진 모델 고려
    #         "messages": [
    #             {"role": "system", "content": f"You are an expert multilingual assistant specializing in news translation and summarization."},
    #             {"role": "user", "content": content_summary_prompt}
    #         ]
    #     }
    #     summary_response = requests.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=summary_payload, timeout=120) # 요약은 더 오래 걸릴 수 있음
    #     summary_response.raise_for_status()
    #     korean_summary = summary_response.json()['choices'][0]['message']['content'].strip()
    #
    #     logging.info(f"API를 통해 '{english_title}' 번역 및 요약 성공.")
    #     return korean_title, korean_summary
    #
    # except requests.RequestException as e:
    #     logging.error(f"API 요청 중 오류 발생 ({english_title}): {e}")
    # except (KeyError, IndexError, TypeError) as e:
    #     logging.error(f"API 응답 처리 중 오류 발생 ({english_title}): {e}. 응답: {title_response.text if 'title_response' in locals() else summary_response.text if 'summary_response' in locals() else 'N/A'}")
    #
    # # API 호출 실패 시 또는 아직 구현 전일 때의 플레이스홀더 반환
    # logging.warning(f"API 호출 실패 또는 로직 미구현으로 플레이스홀더 반환: {english_title}")
    # --- 실제 API 연동 로직 끝 ---

    # 아래는 API 연동 전의 플레이스홀더 반환 값입니다.
    # 사용자가 위 실제 API 로직을 구현하면 이 부분은 제거되거나 주석 처리되어야 합니다.
    korean_title_placeholder = f"[번역된 제목 Placeholder] {english_title}"
    korean_summary_placeholder = (
        f"[번역 및 요약된 본문 Placeholder]\n\n"
        f"이곳에 '{english_title}' 기사의 **실제 한국어 요약 내용**이 와야 합니다.\n"
        f"이 메시지는 개발 및 테스트를 위한 임시 메시지이며, 실제 API 연동이 필요합니다.\n\n"
        f"**원본 영어 내용의 일부 (첫 800자):**\n{english_content[:800]}...\n\n"
        f"---\n**중요:** `scripts/news_updater.py` 파일 내의 `translate_and_summarize_content` 함수에서 "
        f"실제 번역 및 요약 API 호출 로직을 구현하고, 유효한 `TRANSLATION_API_KEY`를 GitHub Secrets에 설정해야 합니다."
    )
    return korean_title_placeholder, korean_summary_placeholder


# --- 주요 실행 로직 ---
def main():
    """
    메인 실행 함수: 뉴스 수집, 처리, Markdown 파일 생성 과정을 총괄합니다.
    """
    logging.info("일일 뉴스 업데이트 스크립트 시작.")
    
    # 출력 디렉토리(_posts) 생성 (없으면)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logging.info(f"Markdown 파일 저장 경로: '{os.path.abspath(OUTPUT_DIR)}'")

    # 1. 뉴스 기사 수집
    all_articles = []
    processed_urls = set() # 중복 기사 URL 추적용
    
    cutoff_date = datetime.now(timezone.utc) - timedelta(hours=HOURS_AGO) # 수집 기준 시간

    logging.info(f"{len(NEWS_SOURCES)}개 뉴스 소스에서 기사 수집 시작 (최근 {HOURS_AGO}시간 기준).")
    for source in NEWS_SOURCES:
        logging.info(f"'{source['name']}' ({source['url']})에서 RSS 피드 파싱 중...")
        try:
            feed = feedparser.parse(source['url'])
            if feed.bozo: # feedparser가 파싱 중 문제를 감지했을 경우
                logging.warning(f"'{source['name']}' 피드 파싱 문제: {feed.bozo_exception}")
            
            articles_from_source = 0
            for entry in feed.entries:
                if articles_from_source >= MAX_ARTICLES_PER_SOURCE:
                    break # 소스당 최대 기사 수 제한

                original_url = entry.get('link')
                if not original_url or original_url in processed_urls:
                    continue # URL 없거나 중복이면 건너뛰기

                published_date = get_article_published_date(entry)
                if not published_date or published_date < cutoff_date:
                    continue # 발행일 없거나 너무 오래된 기사면 건너뛰기
                
                title = entry.get('title', '제목 없음').strip()
                summary = entry.get('summary', entry.get('description', '')).strip()
                
                # HTML 태그 제거 (요약 내용에 HTML이 포함된 경우)
                if '<' in summary and '>' in summary:
                    summary_soup = BeautifulSoup(summary, 'html.parser')
                    summary = summary_soup.get_text(separator=' ', strip=True)

                all_articles.append({
                    'title': title,
                    'link': original_url,
                    'published_date_utc': published_date, # UTC 시간으로 저장
                    'summary': summary,
                    'source_name': source['name'],
                    'content_to_translate': summary # 기본값은 요약본
                })
                processed_urls.add(original_url)
                articles_from_source += 1
            logging.info(f"'{source['name']}'에서 {articles_from_source}개 기사 수집 완료.")

        except Exception as e:
            logging.error(f"'{source['name']}' 피드 처리 중 오류: {e}")
    
    logging.info(f"총 {len(all_articles)}개 고유 기사 수집 완료.")

    # 2. 상위 N개 기사 선정 (최신순 정렬 후 선택)
    all_articles.sort(key=lambda x: x['published_date_utc'], reverse=True) # 최신순 정렬
    top_articles = all_articles[:NUM_TOP_ARTICLES]
    logging.info(f"상위 {len(top_articles)}개 기사 선정 완료.")

    # 3. 선택된 기사 처리 및 Markdown 파일 생성
    if not top_articles:
        logging.info("선정된 기사가 없어 Markdown 파일 생성을 건너<0xE3><0x81><0x8A>니다.")
        return

    for article in top_articles:
        logging.info(f"기사 처리 중: '{article['title']}' (출처: {article['source_name']})")

        # RSS 요약이 너무 짧으면 전체 내용 가져오기 시도
        if len(article['summary']) < SUMMARY_MIN_LENGTH:
            logging.info(f"'{article['title']}' 요약이 짧아 전체 내용 가져오기 시도...")
            full_content_md = fetch_full_content_from_url(article['link'])
            if full_content_md:
                article['content_to_translate'] = full_content_md
                logging.info(f"'{article['title']}' 전체 내용 (Markdown) 성공적으로 가져옴.")
            else:
                logging.warning(f"'{article['title']}' 전체 내용 가져오기 실패 또는 내용 부족. 기존 요약 사용.")
        
        # 영어 제목과 내용을 한국어로 번역 및 요약 (플레이스홀더 함수 호출)
        # content_to_translate는 요약본 또는 전체 내용(Markdown 형식)일 수 있음
        korean_title, korean_summary = translate_and_summarize_content(
            article['title'], 
            article['content_to_translate']
        )

        # Markdown 파일 생성
        try:
            # 파일명 생성: YYYY-MM-DD-original-title-slug.md
            # 날짜는 KST 기준으로 변환
            published_date_kst = article['published_date_utc'].astimezone(KST)
            slug = slugify(article['title'])
            filename_date_str = published_date_kst.strftime('%Y-%m-%d')
            filename = f"{filename_date_str}-{slug}.md"
            filepath = os.path.join(OUTPUT_DIR, filename)

            # YAML Front Matter 생성
            # 날짜 형식: YYYY-MM-DD HH:MM:SS +0900 (KST)
            front_matter_date_str = published_date_kst.strftime('%Y-%m-%d %H:%M:%S %z')
            
            # 파일 내용 구성
            markdown_content = f"""---
layout: post
title: "{korean_title.replace('"', '\"')}"
date: "{front_matter_date_str}"
original_title: "{article['title'].replace('"', '\"')}"
original_source_url: "{article['link']}"
source_name: "{article['source_name']}"
tags: ["암호화폐뉴스", "자동업데이트", "{article['source_name'].lower().replace(' ', '')}"]
---

{korean_summary}

---
**원문 출처:** [{article['title']}]({article['link']}) ({article['source_name']})

*본 기사는 자동화 시스템을 통해 해외 뉴스를 번역 및 요약한 내용으로, 일부 표현이 어색하거나 원문과 다를 수 있습니다. 정확한 내용은 원문 링크를 참고해주시기 바랍니다.*
"""
            # 파일 쓰기 (UTF-8 인코딩)
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(markdown_content)
            logging.info(f"Markdown 파일 생성 완료: {filepath}")

        except Exception as e:
            logging.error(f"Markdown 파일 ('{article['title']}') 생성 중 오류: {e}")

    logging.info("일일 뉴스 업데이트 스크립트 완료.")

# 스크립트 직접 실행 시 main() 함수 호출
if __name__ == "__main__":
    main()
