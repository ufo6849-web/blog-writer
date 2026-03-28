"""
bots/shorts/tts_engine.py
역할: 쇼츠 스크립트 텍스트 → 음성(WAV) + 단어별 타임스탬프(JSON) 생성

엔진 우선순위 (shorts_config.json tts.engine_priority):
  1. ElevenLabs    — 최고 품질, ELEVENLABS_API_KEY 필요
  2. Google Cloud TTS — 중간 품질, GOOGLE_TTS_API_KEY 필요
  3. Edge TTS      — 무료 폴백, API 키 불필요

출력:
  data/shorts/tts/{timestamp}.wav
  data/shorts/tts/{timestamp}_timestamps.json
    [{word: str, start: float, end: float}, ...]
"""
import asyncio
import json
import logging
import os
import re
import struct
import tempfile
import wave
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ─── 공통 유틸 ────────────────────────────────────────────────


def _load_config() -> dict:
    cfg_path = Path(__file__).parent.parent.parent / 'config' / 'shorts_config.json'
    if cfg_path.exists():
        return json.loads(cfg_path.read_text(encoding='utf-8'))
    return {}


def _concat_script(script: dict) -> str:
    """스크립트 dict → 읽기용 단일 텍스트. 문장 사이 공백 추가."""
    parts = [script.get('hook', '')]
    parts.extend(script.get('body', []))
    parts.append(script.get('closer', ''))
    return ' '.join(p for p in parts if p)


def _add_pause(wav_path: Path, pause_ms: int = 300) -> None:
    """WAV 파일 끝에 무음 pause_ms 밀리초 추가 (인플레이스)."""
    with wave.open(str(wav_path), 'rb') as wf:
        params = wf.getparams()
        frames = wf.readframes(wf.getnframes())

    silence_frames = int(params.framerate * pause_ms / 1000)
    silence = b'\x00' * silence_frames * params.nchannels * params.sampwidth

    with wave.open(str(wav_path), 'wb') as wf:
        wf.setparams(params)
        wf.writeframes(frames + silence)


def _get_wav_duration(wav_path: Path) -> float:
    with wave.open(str(wav_path), 'rb') as wf:
        return wf.getnframes() / wf.getframerate()


# ─── ElevenLabs ───────────────────────────────────────────────

def _tts_elevenlabs(text: str, output_path: Path, cfg: dict) -> list[dict]:
    """
    ElevenLabs TTS + 단어별 타임스탬프.
    Returns: [{word, start, end}, ...]
    """
    import requests

    api_key = os.environ.get('ELEVENLABS_API_KEY', '')
    if not api_key:
        raise RuntimeError('ELEVENLABS_API_KEY not set')

    el_cfg = cfg.get('tts', {}).get('elevenlabs', {})
    voice_id = el_cfg.get('voice_id', 'pNInz6obpgDQGcFmaJgB')
    model_id = el_cfg.get('model', 'eleven_multilingual_v2')
    stability = el_cfg.get('stability', 0.5)
    similarity = el_cfg.get('similarity_boost', 0.8)
    speed = el_cfg.get('speed', 1.1)

    url = f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps'
    headers = {'xi-api-key': api_key, 'Content-Type': 'application/json'}
    payload = {
        'text': text,
        'model_id': model_id,
        'voice_settings': {
            'stability': stability,
            'similarity_boost': similarity,
            'speed': speed,
        },
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    # 오디오 디코딩
    import base64
    audio_b64 = data.get('audio_base64', '')
    audio_bytes = base64.b64decode(audio_b64)

    # ElevenLabs는 mp3 반환 → wav 변환
    mp3_tmp = output_path.with_suffix('.mp3')
    mp3_tmp.write_bytes(audio_bytes)
    _mp3_to_wav(mp3_tmp, output_path)
    mp3_tmp.unlink(missing_ok=True)

    # 타임스탬프 파싱
    alignment = data.get('alignment', {})
    chars = alignment.get('characters', [])
    starts = alignment.get('character_start_times_seconds', [])
    ends = alignment.get('character_end_times_seconds', [])

    timestamps = _chars_to_words(chars, starts, ends)
    return timestamps


def _chars_to_words(chars: list, starts: list, ends: list) -> list[dict]:
    """ElevenLabs 문자 레벨 타임스탬프 → 단어 레벨."""
    words = []
    cur_word = ''
    cur_start = 0.0
    cur_end = 0.0

    for ch, st, en in zip(chars, starts, ends):
        if ch in (' ', '\n'):
            if cur_word:
                words.append({'word': cur_word, 'start': round(cur_start, 3), 'end': round(cur_end, 3)})
                cur_word = ''
        else:
            if not cur_word:
                cur_start = st
            cur_word += ch
            cur_end = en

    if cur_word:
        words.append({'word': cur_word, 'start': round(cur_start, 3), 'end': round(cur_end, 3)})

    return words


def _mp3_to_wav(mp3_path: Path, wav_path: Path) -> None:
    try:
        from pydub import AudioSegment
        AudioSegment.from_mp3(str(mp3_path)).export(str(wav_path), format='wav')
        return
    except Exception:
        pass

    # ffmpeg 폴백
    import subprocess
    ffmpeg = _get_ffmpeg()
    subprocess.run(
        [ffmpeg, '-y', '-i', str(mp3_path), str(wav_path)],
        check=True, capture_output=True,
    )


def _get_ffmpeg() -> str:
    ffmpeg_env = os.environ.get('FFMPEG_PATH', '')
    if ffmpeg_env and Path(ffmpeg_env).exists():
        return ffmpeg_env
    return 'ffmpeg'


# ─── Google Cloud TTS ─────────────────────────────────────────

def _tts_google_cloud(text: str, output_path: Path, cfg: dict) -> list[dict]:
    """
    Google Cloud TTS (REST API) + SSML time_pointing으로 타임스탬프 추출.
    Returns: [{word, start, end}, ...]
    """
    import requests

    api_key = os.environ.get('GOOGLE_TTS_API_KEY', '')
    if not api_key:
        raise RuntimeError('GOOGLE_TTS_API_KEY not set')

    gc_cfg = cfg.get('tts', {}).get('google_cloud', {})
    voice_name = gc_cfg.get('voice_name', 'ko-KR-Neural2-C')
    speaking_rate = gc_cfg.get('speaking_rate', 1.1)

    # SSML: 단어별 mark 삽입
    words = text.split()
    ssml_parts = []
    for i, w in enumerate(words):
        ssml_parts.append(f'<mark name="w{i}"/>{w}')
    ssml_text = ' '.join(ssml_parts)
    ssml = f'<speak>{ssml_text}<mark name="end"/></speak>'

    url = f'https://texttospeech.googleapis.com/v1beta1/text:synthesize?key={api_key}'
    payload = {
        'input': {'ssml': ssml},
        'voice': {'languageCode': voice_name[:5], 'name': voice_name},
        'audioConfig': {
            'audioEncoding': 'LINEAR16',
            'speakingRate': speaking_rate,
            'sampleRateHertz': 44100,
        },
        'enableTimePointing': ['SSML_MARK'],
    }

    resp = requests.post(url, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    import base64
    audio_bytes = base64.b64decode(data['audioContent'])
    output_path.write_bytes(audio_bytes)

    # 타임스탬프 파싱
    timepoints = data.get('timepoints', [])
    timestamps = _gcloud_marks_to_words(words, timepoints)
    return timestamps


def _gcloud_marks_to_words(words: list[str], timepoints: list[dict]) -> list[dict]:
    """Google Cloud TTS mark 타임포인트 → 단어별 {word, start, end}."""
    mark_map = {tp['markName']: tp['timeSeconds'] for tp in timepoints}
    total_dur = mark_map.get('end', 0.0)

    result = []
    for i, w in enumerate(words):
        start = mark_map.get(f'w{i}', 0.0)
        end = mark_map.get(f'w{i+1}', total_dur)
        result.append({'word': w, 'start': round(start, 3), 'end': round(end, 3)})
    return result


# ─── Edge TTS + Whisper ───────────────────────────────────────

def _tts_edge(text: str, output_path: Path, cfg: dict) -> list[dict]:
    """
    Edge TTS (무료) → WAV 생성 후 Whisper로 단어별 타임스탬프 추출.
    Returns: [{word, start, end}, ...]
    """
    import edge_tts

    edge_cfg = cfg.get('tts', {}).get('edge_tts', {})
    voice = edge_cfg.get('voice', 'ko-KR-SunHiNeural')
    rate = edge_cfg.get('rate', '+10%')

    mp3_tmp = output_path.with_suffix('.mp3')

    async def _generate():
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await communicate.save(str(mp3_tmp))

    asyncio.get_event_loop().run_until_complete(_generate())

    # mp3 → wav
    _mp3_to_wav(mp3_tmp, output_path)
    mp3_tmp.unlink(missing_ok=True)

    # Whisper로 타임스탬프 추출
    timestamps = _whisper_timestamps(output_path)
    return timestamps


def _whisper_timestamps(wav_path: Path) -> list[dict]:
    """openai-whisper를 사용해 단어별 타임스탬프 추출. 없으면 균등 분할."""
    try:
        import whisper  # type: ignore

        model = whisper.load_model('tiny')
        result = model.transcribe(str(wav_path), word_timestamps=True, language='ko')
        words = []
        for seg in result.get('segments', []):
            for w in seg.get('words', []):
                words.append({
                    'word': w['word'].strip(),
                    'start': round(w['start'], 3),
                    'end': round(w['end'], 3),
                })
        if words:
            return words
    except Exception as e:
        logger.warning(f'Whisper 타임스탬프 실패: {e} — 균등 분할 사용')

    return _uniform_timestamps(wav_path)


def _uniform_timestamps(wav_path: Path) -> list[dict]:
    """Whisper 없을 때 균등 분할 타임스탬프 (캡션 품질 저하 감수)."""
    duration = _get_wav_duration(wav_path)
    with wave.open(str(wav_path), 'rb') as wf:
        pass  # just to confirm it's readable

    # WAV 파일에서 텍스트를 다시 알 수 없으므로 빈 리스트 반환
    # (caption_renderer가 균등 분할을 처리)
    return []


# ─── 메인 엔트리포인트 ────────────────────────────────────────

def generate_tts(
    script: dict,
    output_dir: Path,
    timestamp: str,
    cfg: Optional[dict] = None,
) -> tuple[Path, list[dict]]:
    """
    스크립트 dict → WAV + 단어별 타임스탬프.

    Args:
        script:     {hook, body, closer, ...}
        output_dir: data/shorts/tts/
        timestamp:  파일명 prefix (e.g. "20260328_120000")
        cfg:        shorts_config.json dict (없으면 자동 로드)

    Returns:
        (wav_path, timestamps)  — timestamps: [{word, start, end}, ...]
    """
    if cfg is None:
        cfg = _load_config()

    output_dir.mkdir(parents=True, exist_ok=True)
    wav_path = output_dir / f'{timestamp}.wav'
    ts_path = output_dir / f'{timestamp}_timestamps.json'

    text = _concat_script(script)
    pause_ms = cfg.get('tts', {}).get('inter_sentence_pause_ms', 300)
    priority = cfg.get('tts', {}).get('engine_priority', ['elevenlabs', 'google_cloud', 'edge_tts'])

    engine_map = {
        'elevenlabs':   _tts_elevenlabs,
        'google_cloud': _tts_google_cloud,
        'edge_tts':     _tts_edge,
    }

    timestamps: list[dict] = []
    last_error: Optional[Exception] = None

    for engine_name in priority:
        fn = engine_map.get(engine_name)
        if fn is None:
            continue
        try:
            logger.info(f'TTS 엔진 시도: {engine_name}')
            timestamps = fn(text, wav_path, cfg)
            logger.info(f'TTS 완료 ({engine_name}): {wav_path.name}')
            break
        except Exception as e:
            logger.warning(f'TTS 엔진 실패 ({engine_name}): {e}')
            last_error = e
            if wav_path.exists():
                wav_path.unlink()

    if not wav_path.exists():
        raise RuntimeError(f'모든 TTS 엔진 실패. 마지막 오류: {last_error}')

    # 문장 끝 무음 추가
    try:
        _add_pause(wav_path, pause_ms)
    except Exception as e:
        logger.warning(f'무음 추가 실패: {e}')

    # 타임스탬프 저장
    ts_path.write_text(json.dumps(timestamps, ensure_ascii=False, indent=2), encoding='utf-8')
    logger.info(f'타임스탬프 저장: {ts_path.name} ({len(timestamps)}단어)')

    return wav_path, timestamps


def load_timestamps(ts_path: Path) -> list[dict]:
    """저장된 타임스탬프 JSON 로드."""
    return json.loads(ts_path.read_text(encoding='utf-8'))
