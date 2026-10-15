"""Collect a repository's observable DORA data from GitHub REST API.

Production deployments must be created and have deployment_status updates.
Incident durations use issues labeled 'incident' (issue creation -> close).
Missing observations remain null, never silently converted to zero.
"""
import datetime as dt
import html
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

TOKEN = os.getenv('GH_TOKEN', '')
REPO = os.getenv('GITHUB_REPOSITORY', '')
ROOT = Path('output')
NOW = dt.datetime.now(dt.timezone.utc)
SINCE = NOW - dt.timedelta(days=7)


def parse_date(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None


def api(path):
    request = Request('https://api.github.com' + path, headers={
        'Accept': 'application/vnd.github+json',
        'Authorization': f'Bearer {TOKEN}',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'course-dora-collector'
    })
    try:
        with urlopen(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError(f'GitHub API {error.code}: {path} {error.read().decode()[:200]}') from error


def average_or_none(values):
    return round(sum(values) / len(values), 2) if values else None


def collect():
    if not TOKEN or not REPO or '/' not in REPO:
        raise RuntimeError('GH_TOKEN and GITHUB_REPOSITORY are required')
    deployments = api(f'/repos/{REPO}/deployments?environment=production&per_page=100')
    successful, failed = [], []
    for dep in deployments:
        statuses = api(f'/repos/{REPO}/deployments/{dep["id"]}/statuses?per_page=100')
        if not statuses:
            continue
        latest = statuses[0]
        at = parse_date(latest.get('created_at'))
        if at is None or at < SINCE:
            continue
        if latest['state'] == 'success':
            successful.append((dep, at))
        elif latest['state'] in ('failure', 'error'):
            failed.append((dep, at))

    # Each deployment links to a commit. Commit -> successful deployment time.
    lead_times = []
    for dep, deployed_at in successful:
        sha = dep.get('sha')
        if not sha:
            continue
        commit = api(f'/repos/{REPO}/commits/{quote(sha, safe="")}')
        committed_at = parse_date(commit.get('commit', {}).get('committer', {}).get('date'))
        if committed_at and deployed_at >= committed_at:
            lead_times.append(round((deployed_at - committed_at).total_seconds() / 3600, 2))

    # Incident = issue with label 'incident'. Issue creation/closure is a proxy
    # for actual outage start and recovery; issues must be filed promptly.
    issues = api(f'/repos/{REPO}/issues?state=closed&since={quote(SINCE.isoformat())}&per_page=100')
    recoveries = []
    for issue in issues:
        if 'pull_request' in issue:
            continue
        if not any(label['name'].casefold() == 'incident' for label in issue['labels']):
            continue
        started = parse_date(issue.get('created_at'))
        ended = parse_date(issue.get('closed_at'))
        if started and ended and ended >= SINCE and ended >= started:
            recoveries.append(round((ended - started).total_seconds() / 3600, 2))

    total = len(successful) + len(failed)
    metrics = {
        'repository': REPO,
        'generated_at_utc': NOW.isoformat(),
        'window_days': 7,
        'lead_time_hours': average_or_none(lead_times),
        'deployment_frequency_per_week': len(successful),
        'mttr_hours': average_or_none(recoveries),
        'change_failure_rate_percent': round(100 * len(failed) / total, 2) if total else None,
        'observations': {
            'successful_production_deployments': len(successful),
            'failed_production_deployments': len(failed),
            'commit_to_deployment_samples': len(lead_times),
            'closed_incident_issues': len(recoveries),
        },
        'limitations': [
            'Counts only up to 100 recent production deployments and closed issues.',
            'Lead time uses the deployed SHA committer timestamp; this is a practical approximation.',
            'MTTR uses incident issue creation/closure times as a proxy for incident timing.',
            'Failure rate counts latest failure/error deployment statuses; post-success rollbacks are not automatically captured.',
            'Deployment frequency is successful production deployments, not build or push count.',
            'No production deployments => lead time and failure rate are null, not 0.',
        ],
    }
    return metrics


def display(value, unit=''):
    return '데이터 없음' if value is None else f'{value:g}{unit}'


def svg(metrics):
    entries = [
        ('Lead Time', display(metrics['lead_time_hours'], '시간'), '커밋에서 성공적인 배포까지'),
        ('Deployment Frequency', display(metrics['deployment_frequency_per_week'], '회/주'), '프로덕션 성공 배포'),
        ('MTTR', display(metrics['mttr_hours'], '시간'), 'incident 이슈 생성부터 종료'),
        ('Change Failure Rate', display(metrics['change_failure_rate_percent'], '%'), '실패 / (성공 + 실패) 배포'),
    ]
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="900" height="480" viewBox="0 0 900 480">',
             '<rect width="900" height="480" rx="18" fill="#0f172a"/>',
             '<text x="36" y="58" fill="#f8fafc" font-size="30" font-family="Arial,sans-serif" font-weight="bold">DORA Metrics Dashboard</text>',
             '<text x="36" y="89" fill="#94a3b8" font-size="16" font-family="Arial,sans-serif">Last 7 days · Production only · Missing values are not zero</text>']
    for i, (title, value, caption) in enumerate(entries):
        x = 36 + (i % 2) * 427
        y = 116 + (i // 2) * 164
        parts += [f'<rect x="{x}" y="{y}" width="402" height="144" rx="15" fill="#1e293b"/>',
                  f'<text x="{x+22}" y="{y+32}" fill="#cbd5e1" font-size="18" font-family="Arial,sans-serif">{html.escape(title)}</text>',
                  f'<text x="{x+22}" y="{y+80}" fill="#67e8f9" font-size="30" font-family="Arial,sans-serif" font-weight="bold">{html.escape(value)}</text>',
                  f'<text x="{x+22}" y="{y+116}" fill="#94a3b8" font-size="14" font-family="Arial,sans-serif">{html.escape(caption)}</text>']
    parts += ['<text x="36" y="455" fill="#94a3b8" font-size="13" font-family="Arial,sans-serif">Real deployment and incident records required for meaningful measurements</text>', '</svg>']
    return '\n'.join(parts) + '\n'


def report(m):
    return f'''# DORA 주간 보고서\n\n- 저장소: `{m['repository']}`\n- 기준: 최근 7일, {m['generated_at_utc']} 기준\n\n| 지표 | 결과 |\n|---|---|\n| Lead Time | {display(m['lead_time_hours'], '시간')} |\n| Deployment Frequency | {display(m['deployment_frequency_per_week'], '회/주')} |\n| MTTR | {display(m['mttr_hours'], '시간')} |\n| Change Failure Rate | {display(m['change_failure_rate_percent'], '%')} |\n\n**주의:** `데이터 없음`은 0이 아닙니다. GitHub Deployment(`production`) 및 `incident` 라벨 이슈가 있어야 관련 수치가 의미를 갖습니다.\n\n### 측정 한계\n'''+''.join(f'- {item}\n' for item in m['limitations'])


def main():
    ROOT.mkdir(exist_ok=True)
    m = collect()
    (ROOT / 'metrics.json').write_text(json.dumps(m, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (ROOT / 'weekly-report.md').write_text(report(m), encoding='utf-8')
    (ROOT / 'dashboard.svg').write_text(svg(m), encoding='utf-8')
    print(json.dumps(m, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
