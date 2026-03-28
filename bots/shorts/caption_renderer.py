"""
bots/shorts/caption_renderer.py
역할: 단어별 타임스탬프 → ASS 자막 파일 생성 (단어별 하이라이트)

스타일:
  - 기본: 흰색 볼드, 검정 아웃라인 3px
  - 하이라이트: 노란색 (#FFD700) — 현재 발음 중인 단어
  - 훅 텍스트: 중앙 상단, 72px, 1.5초 표시
  - 본문 자막: 하단 200px, 48px, 최대 2줄

출력:
  data/shorts/captions/{timestamp}.ass
"""
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent.parent


def _load_config() -> dict:
    cfg_path = BASE_DIR / 'config' / 'shorts_config.json'
    if cfg_path.exists():
        return json.loads(cfg_path.read_text(encoding='utf-8'))
    return {}


# ─── 색상 변환 ────────────────────────────────────────────────

def _hex_to_ass(hex_color: str, alpha: int = 0) -> str:
    """
    HTML hex (#RRGGBB) → ASS 색상 &HAABBGGRR 변환.
    ASS는 BGR 순서이며 alpha는 00(불투명)~FF(투명).
    """
    c = hex_color.lstrip('#')
    r, g, b = c[0:2], c[2:4], c[4:6]
    return f'&H{alpha:02X}{b}{g}{r}'


# ─── 시간 포맷 ────────────────────────────────────────────────

def _sec_to_ass_time(seconds: float) -> str:
    """초(float) → ASS 시간 포맷 H:MM:SS.cc."""
    cs = int(round(seconds * 100))
    h = cs // 360000
    cs %= 360000
    m = cs // 6000
    cs %= 6000
    s = cs // 100
    cs %= 100
    return f'{h}:{m:02d}:{s:02d}.{cs:02d}'


# ─── ASS 헤더 ────────────────────────────────────────────────

def _ass_header(cfg: dict) -> str:
    cap_cfg = cfg.get('caption', {})
    font_ko = cap_cfg.get('font_ko', 'Pretendard')
    font_size = cap_cfg.get('font_size', 48)
    hook_size = cap_cfg.get('hook_font_size', 72)
    default_color = _hex_to_ass(cap_cfg.get('default_color', '#FFFFFF'))
    highlight_color = _hex_to_ass(cap_cfg.get('highlight_color', '#FFD700'))
    outline_color = _hex_to_ass(cap_cfg.get('outline_color', '#000000'))
    outline_w = cap_cfg.get('outline_width', 3)
    margin_v = cap_cfg.get('position_from_bottom', 200)

    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_ko},{font_size},{default_color},{default_color},{outline_color},&H80000000,-1,0,0,0,100,100,0,0,1,{outline_w},1,2,20,20,{margin_v},1
Style: Highlight,{font_ko},{font_size},{highlight_color},{highlight_color},{outline_color},&H80000000,-1,0,0,0,100,100,0,0,1,{outline_w},1,2,20,20,{margin_v},1
Style: Hook,{font_ko},{hook_size},{default_color},{default_color},{outline_color},&H80000000,-1,0,0,0,100,100,0,0,1,{outline_w+1},2,5,20,20,100,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


# ─── 단어 → 자막 라인 분할 ────────────────────────────────────

def _split_into_lines(words: list[dict], max_chars: int = 18) -> list[list[dict]]:
    """
    단어 리스트 → 라인 리스트 (최대 max_chars 자).
    반환: [[{word, start, end}, ...], ...]
    """
    lines = []
    cur_line: list[dict] = []
    cur_len = 0

    for w in words:
        word_text = w['word']
        if cur_line and cur_len + len(word_text) + 1 > max_chars:
            lines.append(cur_line)
            cur_line = [w]
            cur_len = len(word_text)
        else:
            cur_line.append(w)
            cur_len += len(word_text) + (1 if cur_line else 0)

    if cur_line:
        lines.append(cur_line)

    return lines


def _line_start_end(line: list[dict]) -> tuple[float, float]:
    return line[0]['start'], line[-1]['end']


# ─── ASS 이벤트 생성 ─────────────────────────────────────────

def _word_highlight_event(
    line: list[dict],
    highlight_color_hex: str,
    default_color_hex: str,
    outline_color_hex: str,
    outline_w: int,
) -> str:
    """
    한 라인의 모든 단어에 대해 단어별 하이라이트 오버라이드 태그 생성.
    각 단어 재생 시간 동안 해당 단어만 highlight_color로 표시.
    ASS override tag: {\\c&Hxxxxxx&} 로 색상 변경.

    반환: 단어별 ASS 이벤트 문자열 목록
    """
    hi_ass = _hex_to_ass(highlight_color_hex)
    df_ass = _hex_to_ass(default_color_hex)

    events = []
    for i, w in enumerate(line):
        start_t = w['start']
        end_t = w['end']

        # 전체 라인 텍스트: 현재 단어만 하이라이트
        parts = []
        for j, other in enumerate(line):
            if j == i:
                parts.append(f'{{\\c{hi_ass}}}{other["word"]}{{\\c{df_ass}}}')
            else:
                parts.append(other['word'])
        text = ' '.join(parts)

        event = (
            f'Dialogue: 0,{_sec_to_ass_time(start_t)},{_sec_to_ass_time(end_t)},'
            f'Default,,0,0,0,,{text}'
        )
        events.append(event)

    return '\n'.join(events)


def _hook_event(hook_text: str, duration: float = 1.5) -> str:
    """훅 텍스트 — 중앙 상단, 72px, 1.5초 표시."""
    return (
        f'Dialogue: 1,{_sec_to_ass_time(0.0)},{_sec_to_ass_time(duration)},'
        f'Hook,,0,0,0,,{hook_text}'
    )


# ─── 균등 분할 타임스탬프 폴백 ───────────────────────────────

def _build_uniform_timestamps(script: dict, total_duration: float) -> list[dict]:
    """
    Whisper 타임스탬프 없을 때 텍스트를 균등 시간으로 분할.
    """
    parts = [script.get('hook', '')]
    parts.extend(script.get('body', []))
    parts.append(script.get('closer', ''))
    text = ' '.join(p for p in parts if p)
    words = text.split()

    if not words:
        return []

    dur_per_word = total_duration / len(words)
    return [
        {
            'word': w,
            'start': round(i * dur_per_word, 3),
            'end': round((i + 1) * dur_per_word, 3),
        }
        for i, w in enumerate(words)
    ]


# ─── 메인 엔트리포인트 ────────────────────────────────────────

def render_captions(
    script: dict,
    timestamps: list[dict],
    output_dir: Path,
    timestamp: str,
    wav_duration: float = 0.0,
    cfg: Optional[dict] = None,
) -> Path:
    """
    스크립트 + 단어별 타임스탬프 → ASS 자막 파일 생성.

    Args:
        script:       {hook, body, closer, ...}
        timestamps:   [{word, start, end}, ...] — 비어있으면 균등 분할
        output_dir:   data/shorts/captions/
        timestamp:    파일명 prefix
        wav_duration: TTS 오디오 총 길이 (균등 분할 폴백용)
        cfg:          shorts_config.json dict

    Returns:
        ass_path
    """
    if cfg is None:
        cfg = _load_config()

    output_dir.mkdir(parents=True, exist_ok=True)
    ass_path = output_dir / f'{timestamp}.ass'

    cap_cfg = cfg.get('caption', {})
    max_chars = cap_cfg.get('max_chars_per_line_ko', 18)
    highlight_color = cap_cfg.get('highlight_color', '#FFD700')
    default_color = cap_cfg.get('default_color', '#FFFFFF')
    outline_color = cap_cfg.get('outline_color', '#000000')
    outline_w = cap_cfg.get('outline_width', 3)

    # 타임스탬프 없으면 균등 분할
    if not timestamps:
        logger.warning('단어별 타임스탬프 없음 — 균등 분할 사용 (캡션 품질 저하)')
        if wav_duration <= 0:
            wav_duration = 20.0
        timestamps = _build_uniform_timestamps(script, wav_duration)

    # ASS 헤더
    header = _ass_header(cfg)
    events = []

    # 훅 이벤트 (첫 1.5초 중앙 표시)
    hook_text = script.get('hook', '')
    if hook_text and timestamps:
        hook_end = min(1.5, timestamps[0]['start'] + 1.5) if timestamps else 1.5
        events.append(_hook_event(hook_text, hook_end))

    # 단어별 하이라이트 이벤트
    lines = _split_into_lines(timestamps, max_chars)
    for line in lines:
        if not line:
            continue
        line_event = _word_highlight_event(
            line, highlight_color, default_color, outline_color, outline_w
        )
        events.append(line_event)

    ass_content = header + '\n'.join(events) + '\n'
    ass_path.write_text(ass_content, encoding='utf-8-sig')  # BOM for Windows compatibility
    logger.info(f'ASS 자막 생성: {ass_path.name} ({len(timestamps)}단어, {len(lines)}라인)')
    return ass_path
