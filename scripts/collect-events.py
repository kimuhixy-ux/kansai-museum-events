#!/usr/bin/env python3
"""各館の公開ページから催しを集めて data/events.json に保存する。"""
import argparse
import datetime as dt
import json
import pathlib
import re
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser

USER_AGENT = 'kansai-museum-events/0.1 (personal calendar tool; contact: github.com/kimuhixy-ux)'
# 相手のサーバーに負担をかけないよう、1件ごとに間を空ける
REQUEST_INTERVAL_SECONDS = 3
ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCES_PATH = ROOT / 'data' / 'museums.json'
OUTPUT_PATH = ROOT / 'data' / 'events.json'
NOISE_WORDS = ('開催中', '本日開催', '開催予定', '開催予定の展覧会', '終了', '予告', '募集中', '今後の展覧会', '詳しく見る', '詳しくはこちら', 'もっと見る')
EVENT_WORDS = re.compile('講座|講演|座談会|ワークショップ|シンポジウム|コンサート|映画会|シネマ|スタンプラリー|トーク')
DATE_RE = re.compile(
    r'(?:(?P<y1>20\d\d)\s*[年./-]\s*)?(?P<m1>\d{1,2})\s*[月./-]\s*(?P<d1>\d{1,2})\s*日?'
    r'(?:\s*[a-z]{3}\.)?(?:\s*[（(][^）)]{1,8}[）)])?'
    r'(?:\s*[-‐－ー―–—~～〜]\s*'
    r'(?:(?P<y2>20\d\d)\s*[年./-]\s*)?(?:(?P<m2>\d{1,2})\s*[月./-]\s*)?(?P<d2>\d{1,2})\s*日?)?'
)
TIME_RANGE_RE = re.compile(r'(\d{1,2})[:：](\d{2})\s*[-–~～〜]\s*(\d{1,2})[:：](\d{2})')
TITLE_NOISE_RE = re.compile(r'会場[［\[][^］\]]*[］\]]|【\s*】|^会期[:：]')


class PageParser(HTMLParser):
    """リンクと見出しを、その直後に続く文字列と一緒に順番どおり取り出す。"""

    SKIPPED_TAGS = {'script', 'style', 'noscript', 'head'}
    HEADING_TAGS = {'h1', 'h2', 'h3', 'h4', 'h5'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens = []
        self.skip_depth = 0
        # <a><h3>題名</h3></a> のように入れ子になる館があるため、別々に覚える
        self.open_link = None
        self.open_heading = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIPPED_TAGS:
            self.skip_depth += 1
        elif tag == 'a' and dict(attrs).get('href'):
            self.open_link = {'tag': 'a', 'href': dict(attrs)['href'], 'text': [], 'after': []}
            self.tokens.append(self.open_link)
        elif tag in self.HEADING_TAGS:
            self.open_heading = {'tag': tag, 'href': None, 'text': [], 'after': []}
            self.tokens.append(self.open_heading)

    def handle_endtag(self, tag):
        if tag in self.SKIPPED_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif tag == 'a':
            self.open_link = None
        elif tag in self.HEADING_TAGS:
            self.open_heading = None

    def handle_data(self, data):
        text = data.strip()
        if not text or self.skip_depth or not self.tokens:
            return
        open_tokens = [token for token in (self.open_link, self.open_heading) if token]
        for token in open_tokens:
            token['text'].append(text)
        if not open_tokens:
            self.tokens[-1]['after'].append(text)


def resolve_date(year, month, day, today):
    """年の書かれていない日付は、直近の過去〜未来で最も自然な年に補う。"""
    if year is None:
        year = today.year
        if month < today.month - 6:
            year += 1
    return dt.date(year, month, day)


def date_from_match(match, today):
    has_year = match['y1'] is not None
    # 年も「月」もない「10.30」のような数字は、日付と断定できないので捨てる
    if not has_year and '月' not in match[0]:
        return None
    start = resolve_date(int(match['y1']) if has_year else None, int(match['m1']), int(match['d1']), today)
    if not match['d2']:
        return start, start
    end_month = int(match['m2']) if match['m2'] else start.month
    end_year = int(match['y2']) if match['y2'] else start.year
    end = dt.date(end_year, end_month, int(match['d2']))
    # 「12月20日〜1月11日」のように年をまたぐ会期は年の表記が省かれる
    if end < start and not match['y2']:
        end = dt.date(end_year + 1, end_month, int(match['d2']))
    return start, end


def parse_date_range(text, today, prefer_range=False):
    """見つかった日付と、その直後の文字列（時刻の検索に使う）を返す。"""
    fallback = None
    for match in DATE_RE.finditer(text):
        try:
            found = date_from_match(match, today)
        except ValueError:
            continue
        if not found:
            continue
        result = (found[0], found[1], text[match.end():match.end() + 60])
        if not prefer_range or found[0] != found[1]:
            return result
        fallback = fallback or result
    return fallback


def clean_title(words, remove_pattern=None):
    kept = []
    for word in words:
        if word in NOISE_WORDS or word in kept:
            continue
        match = DATE_RE.search(word)
        if match:
            head = word[:match.start()].strip(' ―-　')
            rest = word[match.end():].strip()
            # 日付の後ろが補足だけなら、そこから先は題名ではない
            is_supplement = not rest or re.match(r'[※（(\d]|会場|受付|開場|時間', rest)
            if head:
                kept.append(head)
            if is_supplement:
                break
            word = rest
        kept.append(word)
    title = TITLE_NOISE_RE.sub(' ', ' '.join(kept))
    if remove_pattern:
        title = re.sub(remove_pattern, ' ', title)
    # 互換文字（⽣など）は検索にかからないので、通常の字に揃える
    title = unicodedata.normalize('NFKC', title)
    title = re.sub(r'\s+', ' ', title)
    title = re.sub(r'(?<!\S)(\S+)(?: \1)+(?!\S)', r'\1', title)
    return title.strip(' 　|｜')[:120]


def text_until_next_link(tokens, index):
    """リンクの直後にある文字列を、途中の見出しも含めて集める。"""
    collected = list(tokens[index]['after'])
    for following in tokens[index + 1:]:
        if following['tag'] == 'a':
            break
        collected += following['text'] + following['after']
    return collected


def link_candidates(tokens, page, final_url):
    href_re = re.compile(page['linkPattern'])
    merged = {}
    for index, token in enumerate(tokens):
        if token['tag'] != 'a':
            continue
        url = urllib.parse.urljoin(final_url, token['href'])
        if not href_re.search(url):
            continue
        # 同じURLに複数の日程が並ぶ館では、リンクごとに別の催しとして扱う
        key = (url, index) if page.get('perLink') else url
        entry = merged.setdefault(key, {'url': url, 'words': [], 'after': []})
        after = text_until_next_link(tokens, index)
        if page.get('titleIncludesAfter'):
            entry['words'] += [word for word in after if not DATE_RE.search(word)]
        entry['words'] += token['text']
        entry['after'] += after
    return merged.values()


def heading_url(tokens, index, page, final_url):
    if page.get('urlFrom') == 'page':
        return final_url
    step = -1 if page.get('urlFrom') == 'previous' else 1
    position = index + step
    while 0 <= position < len(tokens):
        token = tokens[position]
        if token['tag'] == 'a':
            return urllib.parse.urljoin(final_url, token['href'])
        position += step
    return final_url


def heading_candidates(tokens, page, final_url):
    candidates = []
    for index, token in enumerate(tokens):
        if token['tag'] != page['heading']:
            continue
        after = list(token['after'])
        # 見出しと見出しの間の文字列だけを、その催しの情報として読む
        for following in tokens[index + 1:]:
            if following['tag'] == page['heading']:
                break
            after += following['text'] + following['after']
        candidates.append({'url': heading_url(tokens, index, page, final_url), 'words': token['text'], 'after': after})
    return candidates


def extract_events(html, final_url, page, museum, today):
    parser = PageParser()
    parser.feed(html)
    finder = heading_candidates if page.get('heading') else link_candidates
    prefer_range = page.get('preferRange', False)
    events = []
    for candidate in finder(parser.tokens, page, final_url):
        # 見出しに投稿日が混ざる館では、見出しと続きをまとめて会期を探す
        found = (parse_date_range(' '.join(candidate['words'] + candidate['after'])[:200], today, True) if prefer_range
                 else parse_date_range(' '.join(candidate['words']), today)
                 or parse_date_range(' '.join(candidate['after'])[:200], today))
        title = clean_title(candidate['words'], museum.get('titleRemove'))
        if not found or len(title) < 2:
            continue
        start, end, trailing = found
        is_event = page['kind'] == 'event' or start == end or EVENT_WORDS.search(title)
        event = {
            'id': f'{candidate["url"]}@{start.isoformat()}', 'title': title,
            'kind': 'event' if is_event else 'exhibition', 'museumId': museum['id'],
            'start': start.isoformat(), 'end': end.isoformat(), 'url': candidate['url'],
        }
        times = TIME_RANGE_RE.search(trailing)
        if times and start == end:
            event['startTime'] = f'{int(times[1]):02d}:{times[2]}'
            event['endTime'] = f'{int(times[3]):02d}:{times[4]}'
        events.append(event)
    return events


def build_ssl_context():
    # 民博のサーバーは途中の証明書を送らず、Linux では検証に失敗する。検証は止めず、不足分だけ補う
    context = ssl.create_default_context()
    context.load_verify_locations(pathlib.Path(__file__).with_name('extra-ca-certs.pem'))
    return context


SSL_CONTEXT = build_ssl_context()


def fetch(url, robots_cache):
    origin = '{0.scheme}://{0.netloc}'.format(urllib.parse.urlsplit(url))
    if origin not in robots_cache:
        parser = urllib.robotparser.RobotFileParser()
        try:
            request = urllib.request.Request(origin + '/robots.txt', headers={'User-Agent': USER_AGENT})
            with urllib.request.urlopen(request, timeout=30, context=SSL_CONTEXT) as response:
                parser.parse(response.read().decode('utf-8', 'ignore').splitlines())
        except urllib.error.HTTPError as error:
            # 404はルールなしだが、それ以外の失敗は「許可」と見なさない
            parser.parse([] if error.code == 404 else ['User-agent: *', 'Disallow: /'])
        robots_cache[origin] = parser
        time.sleep(REQUEST_INTERVAL_SECONDS)
    if not robots_cache[origin].can_fetch(USER_AGENT, url):
        raise PermissionError(f'robots.txt で禁止: {url}')
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=30, context=SSL_CONTEXT) as response:
        charset = response.headers.get_content_charset() or 'utf-8'
        return response.read().decode(charset, 'ignore'), response.geturl()


def collect_museum(museum, robots_cache, today):
    events, errors = {}, []
    for page in museum.get('pages', []):
        try:
            html, final_url = fetch(page['url'], robots_cache)
            for event in extract_events(html, final_url, page, museum, today):
                if event['end'] >= today.isoformat():
                    events.setdefault(event['id'], event)
        except (urllib.error.URLError, PermissionError, TimeoutError, ValueError) as error:
            errors.append(f'{page["url"]}: {error}')
        time.sleep(REQUEST_INTERVAL_SECONDS)
    return events, errors


def load_previous_events(today):
    if not OUTPUT_PATH.exists():
        return []
    previous = json.loads(OUTPUT_PATH.read_text(encoding='utf-8'))
    return [event for event in previous['events'] if event['end'] >= today.isoformat()]


def main():
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument('--dry-run', action='store_true', help='保存せず結果だけ表示する')
    args = arg_parser.parse_args()
    today = dt.date.today()
    museums = json.loads(SOURCES_PATH.read_text(encoding='utf-8'))
    previous = load_previous_events(today)
    robots_cache, events, statuses = {}, {}, {}
    for museum in museums:
        found, errors = collect_museum(museum, robots_cache, today)
        # 一時的な障害で予定が消えないよう、取れなかった館は前回の結果を残す
        if museum.get('pages') and not found:
            found = {event['id']: event for event in previous if event['museumId'] == museum['id']}
        events.update(found)
        statuses[museum['id']] = {'count': len(found), 'errors': errors}
        print(f'{museum["name"]}: {len(found)}件 {errors or ""}', file=sys.stderr)
    ordered = sorted(events.values(), key=lambda event: (event['start'], event['title']))
    if args.dry_run:
        for event in ordered:
            time_label = event.get('startTime', '')
            print(event['start'], event['end'], time_label, event['kind'], event['museumId'], event['title'], sep=' | ')
        return
    payload = {'updatedAt': today.isoformat(), 'statuses': statuses, 'events': ordered}
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
