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
import openai
from dotenv import load_dotenv # 로컬 환경 변수 로드용

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

if os.path.exists('.env'):
    load_dotenv()

NEWS_SOURCES = [
    {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "Cointelegraph", "url": "https://cointelegraph.com/rss"},
    {"name": "Decrypt", "url": "https://decrypt.co/feed"},
    {"name": "Bitcoin.com News", "url": "https://news.bitcoin.com/feed/"},
    {"name": "BeInCrypto", "url": "https://beincrypto.com/feed/"}
]

OUTPUT_DIR = "_posts"
MAX_ARTICLES_PER_SOURCE = 10
NUM_TOP_ARTICLES = 3
HOURS_AGO = 24
SUMMARY_MIN_LENGTH = 250 
KST = pytz.timezone('Asia/Seoul')

def slugify(text):
    text = text.lower()
    text = re.sub(r'\s+', '-', text)
    text = re.sub(r'[^\w\-]+', '', text)
    text = re.sub(r'\-\-+', '-', text)
    text = text.strip('-')
    return text[:70]

def get_article_published_date(entry):
    date_fields = ['published_parsed', 'updated_parsed']
    parsed_time = None
    for field in date_fields:
        if hasattr(entry, field) and entry[field]:
            parsed_time = entry[field]
            break
    
    if parsed_time:
        try:
            return datetime(*parsed_time[:6], tzinfo=timezone.utc)
        except Exception as e:
            logging.warning(f"날짜 변환 중 오류 ({entry.get('link', 'N/A')}): {e}")
    return None

def fetch_full_content_from_url(article_url):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(article_url, timeout=20, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        content_selectors = [
            'article.article-content', 'div.article-content', 'div.post-content', 
            'div.entry-content', 'section.article__body', 'div.main-content',
            'article', 'main' 
        ]
        article_html_content = None
        for selector in content_selectors:
            element = soup.select_one(selector)
            if element:
                for el_to_remove in element.select('script, style, nav, footer, aside, .ad, .advertisement, .related-articles, .comments-area'):
                    el_to_remove.decompose()
                article_html_content = str(element)
                break
        if not article_html_content and soup.body:
            logging.warning(f"특정 콘텐츠 영역을 찾지 못해 body 전체를 사용합니다: {article_url}")
            article_html_content = str(soup.body)
        if article_html_content:
            markdown_text = markdownify.markdownify(article_html_content, heading_style='ATX', bullets='*').strip()
            if len(markdown_text) < SUMMARY_MIN_LENGTH:
                 logging.warning(f"추출된 전체 내용이 너무 짧습니다 ({len(markdown_text)}자): {article_url}")
                 return None
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

def translate_and_summarize_content(english_title, english_content, target_language="ko"):
    api_key = os.getenv("TRANSLATION_API_KEY")
    if not api_key:
        logging.warning("TRANSLATION_API_KEY 환경 변수가 설정되지 않았습니다.")
        return "[미번역] " + english_title, english_content

    try:
        # 최신 openai(1.x.x) 방식: client 인스턴스 생성
        client = openai.OpenAI(api_key=api_key)

        # 1. 제목 번역
        title_prompt = f"Translate this news headline to natural and concise Korean:\n{english_title}"
        title_response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": title_prompt}],
            timeout=30
        )
        korean_title = title_response.choices[0].message.content.strip()

        # 2. 본문 번역 및 요약
        summary_prompt = (
            f"다음은 영어 뉴스 기사입니다. 내용을 한국어로 번역한 후, 핵심 정보와 주요 인사이트 위주로 3~5개의 단락으로 짧고 명확하게 요약해 주세요. 결과물에는 반드시 한국어 요약만 남겨 주세요.\n\n"
            f"뉴스 기사:\n{english_content[:3000]}"
        )
        summary_response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": summary_prompt}],
            timeout=60
        )
        korean_summary = summary_response.choices[0].message.content.strip()
        return korean_title, korean_summary
    except Exception as e:
        logging.error(f"OpenAI API 호출 중 오류: {e}")
        return "[API 오류] " + english_title, "[API 오류] 내용 생성 실패"

def main():
    logging.info("일일 뉴스 업데이트 스크립트 시작.")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    logging.info(f"Markdown 파일 저장 경로: '{os.path.abspath(OUTPUT_DIR)}'")
    all_articles = []
    processed_urls = set()
    cutoff_date = datetime.now(timezone.utc) - timedelta(hours=HOURS_AGO)
    logging.info(f"{len(NEWS_SOURCES)}개 뉴스 소스에서 기사 수집 시작 (최근 {HOURS_AGO}시간 기준).")
    for source in NEWS_SOURCES:
        logging.info(f"'{source['name']}' ({source['url']})에서 RSS 피드 파싱 중...")
        try:
            feed = feedparser.parse(source['url'])
            if feed.bozo:
                logging.warning(f"'{source['name']}' 피드 파싱 문제: {feed.bozo_exception}")
            articles_from_source = 0
            for entry in feed.entries:
                if articles_from_source >= MAX_ARTICLES_PER_SOURCE:
                    break
                original_url = entry.get('link')
                if not original_url or original_url in processed_urls:
                    continue
                published_date = get_article_published_date(entry)
                if not published_date or published_date < cutoff_date:
                    continue
                title = entry.get('title', '제목 없음').strip()
                summary = entry.get('summary', entry.get('description', '')).strip()
                if '<' in summary and '>' in summary:
                    summary_soup = BeautifulSoup(summary, 'html.parser')
                    summary = summary_soup.get_text(separator=' ', strip=True)
                all_articles.append({
                    'title': title,
                    'link': original_url,
                    'published_date_utc': published_date,
                    'summary': summary,
                    'source_name': source['name'],
                    'content_to_translate': summary
                })
                processed_urls.add(original_url)
                articles_from_source += 1
            logging.info(f"'{source['name']}'에서 {articles_from_source}개 기사 수집 완료.")
        except Exception as e:
            logging.error(f"'{source['name']}' 피드 처리 중 오류: {e}")
    logging.info(f"총 {len(all_articles)}개 고유 기사 수집 완료.")
    all_articles.sort(key=lambda x: x['published_date_utc'], reverse=True)
    top_articles = all_articles[:NUM_TOP_ARTICLES]
    logging.info(f"상위 {len(top_articles)}개 기사 선정 완료.")
    if not top_articles:
        logging.info("선정된 기사가 없어 Markdown 파일 생성을 건너뜁니다.")
        return
    for article in top_articles:
        logging.info(f"기사 처리 중: '{article['title']}' (출처: {article['source_name']})")
        if len(article['summary']) < SUMMARY_MIN_LENGTH:
            logging.info(f"'{article['title']}' 요약이 짧아 전체 내용 가져오기 시도...")
            full_content_md = fetch_full_content_from_url(article['link'])
            if full_content_md:
                article['content_to_translate'] = full_content_md
                logging.info(f"'{article['title']}' 전체 내용 (Markdown) 성공적으로 가져옴.")
            else:
                logging.warning(f"'{article['title']}' 전체 내용 가져오기 실패 또는 내용 부족. 기존 요약 사용.")
        korean_title, korean_summary = translate_and_summarize_content(
            article['title'],
            article['content_to_translate']
        )
        try:
            published_date_kst = article['published_date_utc'].astimezone(KST)
            slug = slugify(article['title'])
            filename_date_str = published_date_kst.strftime('%Y-%m-%d')
            filename = f"{filename_date_str}-{slug}.md"
            filepath = os.path.join(OUTPUT_DIR, filename)
            front_matter_date_str = published_date_kst.strftime('%Y-%m-%d %H:%M:%S %z')
            # ==== 여기서 백슬래시 치환은 f-string 바깥에서 처리 ====
            safe_korean_title = korean_title.replace('"', '\\"')
            safe_original_title = article['title'].replace('"', '\\"')
            # =====================================================
            markdown_content = (
                f"---\n"
                f"layout: post\n"
                f"title: \"{safe_korean_title}\"\n"
                f"date: \"{front_matter_date_str}\"\n"
                f"original_title: \"{safe_original_title}\"\n"
                f"original_source_url: \"{article['link']}\"\n"
                f"source_name: \"{article['source_name']}\"\n"
                f"tags: [\"암호화폐뉴스\", \"자동업데이트\", \"{article['source_name'].lower().replace(' ', '')}\"]\n"
                f"---\n\n"
                f"{korean_summary}\n\n"
                f"---\n"
                f"**원문 출처:** [{article['title']}]({article['link']}) ({article['source_name']})\n\n"
                f"*본 기사는 자동화 시스템을 통해 해외 뉴스를 번역 및 요약한 내용으로, 일부 표현이 어색하거나 원문과 다를 수 있습니다. 정확한 내용은 원문 링크를 참고해주시기 바랍니다.*\n"
            )
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(markdown_content)
            logging.info(f"Markdown 파일 생성 완료: {filepath}")
        except Exception as e:
            logging.error(f"Markdown 파일 ('{article['title']}') 생성 중 오류: {e}")
    logging.info("일일 뉴스 업데이트 스크립트 완료.")
    


if __name__ == "__main__":
    main()

