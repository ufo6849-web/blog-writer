"""
bots/shorts/video_assembler.py
역할: 준비된 클립 + TTS 오디오 + ASS 자막 → 최종 쇼츠 MP4 조립

FFmpeg 전용 (CapCut 없음):
  1. 각 클립을 오디오 길이에 맞게 비율 배분
  2. xfade crossfade로 연결
  3. ASS 자막 burn-in
  4. TTS 오디오 합성 + BGM 덕킹
  5. 페이드인/페이드아웃
  6. 루프 최적화: 마지막 클립 = 첫 클립 복사 (리플레이 유도)

출력:
  data/shorts/rendered/{timestamp}.mp4
"""
import json
import logging
import os
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent.parent


def _load_config() -> dict:
    cfg_path = BASE_DIR / 'config' / 'shorts_config.json'
    if cfg_path.exists():
        return json.loads(cfg_path.read_text(encoding='utf-8'))
    return {}


def _get_ffmpeg() -> str:
    ffmpeg_env = os.environ.get('FFMPEG_PATH', '')
    if ffmpeg_env and Path(ffmpeg_env).exists():
        return ffmpeg_env
    return 'ffmpeg'


def _get_wav_duration(wav_path: Path) -> float:
    try:
        with wave.open(str(wav_path), 'rb') as wf:
            return wf.getnframes() / wf.getframerate()
    except Exception:
        # ffprobe 폴백
        try:
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                 '-of', 'default=noprint_wrappers=1:nokey=1', str(wav_path)],
                capture_output=True, text=True, timeout=10,
            )
            return float(result.stdout.strip())
        except Exception:
            return 20.0


def _get_video_duration(video_path: Path) -> float:
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', str(video_path)],
            capture_output=True, text=True, timeout=10,
        )
        return float(result.stdout.strip())
    except Exception:
        return 6.0


# ─── 클립 연결 ────────────────────────────────────────────────

def _trim_clip(src: Path, dst: Path, duration: float, ffmpeg: str) -> bool:
    """클립을 duration 초로 트리밍."""
    cmd = [
        ffmpeg, '-y', '-i', str(src),
        '-t', f'{duration:.3f}',
        '-c:v', 'libx264', '-crf', '23', '-preset', 'fast',
        '-an', '-r', '30',
        str(dst),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        return True
    except subprocess.CalledProcessError as e:
        logger.warning(f'트리밍 실패: {e.stderr.decode(errors="ignore")[:200]}')
        return False


def _concat_with_xfade(clips: list[Path], output: Path, crossfade: float, ffmpeg: str) -> bool:
    """
    xfade 트랜지션으로 클립 연결.
    2개 이상 클립의 경우 순차 xfade 적용.
    """
    if len(clips) == 1:
        import shutil
        shutil.copy2(str(clips[0]), str(output))
        return True

    # 각 클립 길이 확인
    durations = [_get_video_duration(c) for c in clips]

    # ffmpeg complex filtergraph 구성
    inputs = []
    for c in clips:
        inputs += ['-i', str(c)]

    # xfade chain: [0][1]xfade, [xfade1][2]xfade, ...
    filter_parts = []
    offset = 0.0
    prev_label = '[0:v]'

    for i in range(1, len(clips)):
        offset += durations[i - 1] - crossfade
        out_label = f'[xf{i}]'
        filter_parts.append(
            f'{prev_label}[{i}:v]xfade=transition=fade:duration={crossfade}:offset={offset:.3f}{out_label}'
        )
        prev_label = out_label

    filter_complex = ';'.join(filter_parts)

    cmd = [
        ffmpeg, '-y',
        *inputs,
        '-filter_complex', filter_complex,
        '-map', prev_label,
        '-c:v', 'libx264', '-crf', '23', '-preset', 'fast',
        '-an', '-r', '30',
        str(output),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        return True
    except subprocess.CalledProcessError as e:
        logger.warning(f'xfade 연결 실패: {e.stderr.decode(errors="ignore")[:300]}')
        # 폴백: 단순 concat (트랜지션 없음)
        return _concat_simple(clips, output, ffmpeg)


def _concat_simple(clips: list[Path], output: Path, ffmpeg: str) -> bool:
    """트랜지션 없는 단순 concat (폴백)."""
    list_file = output.parent / 'concat_list.txt'
    lines = [f"file '{c.as_posix()}'" for c in clips]
    list_file.write_text('\n'.join(lines), encoding='utf-8')

    cmd = [
        ffmpeg, '-y',
        '-f', 'concat', '-safe', '0',
        '-i', str(list_file),
        '-c:v', 'libx264', '-crf', '23', '-preset', 'fast',
        '-an', '-r', '30',
        str(output),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        list_file.unlink(missing_ok=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f'단순 concat 실패: {e.stderr.decode(errors="ignore")[:200]}')
        list_file.unlink(missing_ok=True)
        return False


# ─── 오디오 합성 ─────────────────────────────────────────────

def _mix_audio(tts_wav: Path, bgm_path: Optional[Path], bgm_db: float,
               total_dur: float, output: Path, ffmpeg: str) -> bool:
    """TTS + BGM 혼합 (BGM 덕킹)."""
    if bgm_path and bgm_path.exists():
        cmd = [
            ffmpeg, '-y',
            '-i', str(tts_wav),
            '-stream_loop', '-1', '-i', str(bgm_path),
            '-filter_complex', (
                f'[1:a]volume={bgm_db}dB,atrim=0:{total_dur:.3f}[bgm];'
                f'[0:a][bgm]amix=inputs=2:duration=first[aout]'
            ),
            '-map', '[aout]',
            '-c:a', 'aac', '-b:a', '192k',
            '-t', f'{total_dur:.3f}',
            str(output),
        ]
    else:
        cmd = [
            ffmpeg, '-y',
            '-i', str(tts_wav),
            '-c:a', 'aac', '-b:a', '192k',
            '-t', f'{total_dur:.3f}',
            str(output),
        ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        return True
    except subprocess.CalledProcessError as e:
        logger.warning(f'오디오 혼합 실패: {e.stderr.decode(errors="ignore")[:200]}')
        return False


# ─── 최종 합성 ────────────────────────────────────────────────

def _assemble_final(
    video: Path, audio: Path, ass_path: Optional[Path],
    output: Path, fade_in: float, fade_out: float,
    total_dur: float, cfg: dict, ffmpeg: str,
) -> bool:
    """
    비디오 + 오디오 + ASS 자막 → 최종 MP4.
    페이드인/아웃 + 루프 최적화 (0.2s 무음 끝에 추가).
    """
    vid_cfg = cfg.get('video', {})
    crf = vid_cfg.get('crf', 18)
    codec = vid_cfg.get('codec', 'libx264')
    audio_codec = vid_cfg.get('audio_codec', 'aac')
    audio_bitrate = vid_cfg.get('audio_bitrate', '192k')

    # 페이드인/아웃 필터
    fade_filter = (
        f'fade=t=in:st=0:d={fade_in},'
        f'fade=t=out:st={total_dur - fade_out:.3f}:d={fade_out}'
    )

    # ASS 자막 burn-in
    if ass_path and ass_path.exists():
        ass_posix = ass_path.as_posix().replace(':', '\\:')
        vf = f'{fade_filter},ass={ass_posix}'
    else:
        vf = fade_filter

    cmd = [
        ffmpeg, '-y',
        '-i', str(video),
        '-i', str(audio),
        '-vf', vf,
        '-af', (
            f'afade=t=in:st=0:d={fade_in},'
            f'afade=t=out:st={total_dur - fade_out:.3f}:d={fade_out},'
            f'apad=pad_dur=0.2'  # 루프 최적화: 0.2s 무음
        ),
        '-c:v', codec, '-crf', str(crf), '-preset', 'medium',
        '-c:a', audio_codec, '-b:a', audio_bitrate,
        '-r', str(vid_cfg.get('fps', 30)),
        '-shortest',
        str(output),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f'최종 합성 실패: {e.stderr.decode(errors="ignore")[:400]}')
        return False


# ─── 파일 크기 체크 ──────────────────────────────────────────

def _check_filesize(path: Path, max_mb: int = 50) -> bool:
    size_mb = path.stat().st_size / (1024 * 1024)
    logger.info(f'출력 파일 크기: {size_mb:.1f}MB')
    return size_mb <= max_mb


def _rerender_smaller(src: Path, dst: Path, ffmpeg: str) -> bool:
    """파일 크기 초과 시 CRF 23으로 재인코딩."""
    cmd = [
        ffmpeg, '-y', '-i', str(src),
        '-c:v', 'libx264', '-crf', '23', '-preset', 'medium',
        '-c:a', 'aac', '-b:a', '128k',
        str(dst),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f'재인코딩 실패: {e.stderr.decode(errors="ignore")[:200]}')
        return False


# ─── 메인 엔트리포인트 ────────────────────────────────────────

def assemble(
    clips: list[Path],
    tts_wav: Path,
    ass_path: Optional[Path],
    output_dir: Path,
    timestamp: str,
    cfg: Optional[dict] = None,
    work_dir: Optional[Path] = None,
) -> Path:
    """
    클립 + TTS + 자막 → 최종 쇼츠 MP4.

    Args:
        clips:      [clip_path, ...] — 준비된 1080×1920 MP4 목록
        tts_wav:    TTS 오디오 WAV 경로
        ass_path:   ASS 자막 경로 (None이면 자막 없음)
        output_dir: data/shorts/rendered/
        timestamp:  파일명 prefix
        cfg:        shorts_config.json dict
        work_dir:   임시 작업 디렉터리 (None이면 자동 생성)

    Returns:
        rendered_path

    Raises:
        RuntimeError — 조립 실패 또는 품질 게이트 미통과
    """
    if cfg is None:
        cfg = _load_config()

    output_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = _get_ffmpeg()

    vid_cfg = cfg.get('video', {})
    crossfade = vid_cfg.get('crossfade_sec', 0.3)
    fade_in = vid_cfg.get('fade_in_sec', 0.5)
    fade_out = vid_cfg.get('fade_out_sec', 0.5)
    bgm_path_str = vid_cfg.get('bgm_path', '')
    bgm_db = vid_cfg.get('bgm_volume_db', -18)
    bgm_path = BASE_DIR / bgm_path_str if bgm_path_str else None

    audio_dur = _get_wav_duration(tts_wav)
    logger.info(f'TTS 길이: {audio_dur:.1f}초')

    # 품질 게이트: 15초 미만 / 60초 초과
    if audio_dur < 10:
        raise RuntimeError(f'TTS 길이 너무 짧음: {audio_dur:.1f}초 (최소 10초)')
    if audio_dur > 65:
        raise RuntimeError(f'TTS 길이 너무 김: {audio_dur:.1f}초 (최대 65초)')

    if not clips:
        raise RuntimeError('클립 없음 — 조립 불가')

    # 임시 작업 디렉터리
    import contextlib
    import shutil

    tmp_cleanup = work_dir is None
    if work_dir is None:
        work_dir = output_dir / f'_work_{timestamp}'
        work_dir.mkdir(parents=True, exist_ok=True)

    try:
        # ── 루프 최적화: 클립 목록 끝에 첫 클립 추가 ──────────────
        loop_clips = list(clips)
        if len(clips) > 1:
            loop_clip = work_dir / 'loop_clip.mp4'
            if _trim_clip(clips[0], loop_clip, min(2.0, _get_video_duration(clips[0])), ffmpeg):
                loop_clips.append(loop_clip)

        # ── 클립 길이 배분 ────────────────────────────────────────
        total_clip_dur = audio_dur + fade_in + fade_out
        n = len(loop_clips)
        base_dur = total_clip_dur / n
        clip_dur = max(3.0, min(base_dur, 8.0))

        # 각 클립 트리밍
        trimmed = []
        for i, clip in enumerate(loop_clips):
            t = work_dir / f'trimmed_{i:02d}.mp4'
            src_dur = _get_video_duration(clip)
            actual_dur = min(clip_dur, src_dur)
            if actual_dur < 1.0:
                actual_dur = src_dur
            if _trim_clip(clip, t, actual_dur, ffmpeg):
                trimmed.append(t)
            else:
                logger.warning(f'클립 {i} 트리밍 실패 — 건너뜀')

        if not trimmed:
            raise RuntimeError('트리밍된 클립 없음')

        # ── 클립 연결 ─────────────────────────────────────────────
        concat_out = work_dir / 'concat.mp4'
        if not _concat_with_xfade(trimmed, concat_out, crossfade, ffmpeg):
            raise RuntimeError('클립 연결 실패')

        # ── 오디오 혼합 ───────────────────────────────────────────
        audio_out = work_dir / 'audio_mixed.aac'
        if not _mix_audio(tts_wav, bgm_path, bgm_db, audio_dur + 0.2, audio_out, ffmpeg):
            # BGM 없이 TTS만
            audio_out = tts_wav

        # ── 최종 합성 ─────────────────────────────────────────────
        final_out = output_dir / f'{timestamp}.mp4'
        if not _assemble_final(
            concat_out, audio_out, ass_path,
            final_out, fade_in, fade_out, audio_dur,
            cfg, ffmpeg,
        ):
            raise RuntimeError('최종 합성 실패')

        # ── 파일 크기 게이트 ──────────────────────────────────────
        if not _check_filesize(final_out, max_mb=50):
            logger.warning('파일 크기 초과 (>50MB) — CRF 23으로 재인코딩')
            rerender_out = output_dir / f'{timestamp}_small.mp4'
            if _rerender_smaller(final_out, rerender_out, ffmpeg):
                final_out.unlink()
                rerender_out.rename(final_out)

        # ── 최종 길이 검증 ─────────────────────────────────────────
        final_dur = _get_video_duration(final_out)
        if final_dur < 10:
            raise RuntimeError(f'최종 영상 길이 너무 짧음: {final_dur:.1f}초')
        if final_dur > 65:
            logger.warning(f'최종 영상 길이 초과: {final_dur:.1f}초 (YouTube Shorts 제한 60초)')

        logger.info(f'쇼츠 조립 완료: {final_out.name} ({final_dur:.1f}초)')
        return final_out

    finally:
        if tmp_cleanup and work_dir.exists():
            import shutil
            shutil.rmtree(work_dir, ignore_errors=True)
