import { useRef, useState } from 'react'
import { apiFetch, ApiError, mediaUrl } from '../api'
import { formatDuration } from '../format'
import { Icon } from './Icon'

const RATES = [1, 1.5, 2] as const
const BARS = 30

/** Bar heights (0.25 to 1) from the message id: a stable stand-in until real waveform data exists. */
function barHeights(seed: string): number[] {
  let h = 2166136261
  for (const ch of seed) h = Math.imul(h ^ ch.charCodeAt(0), 16777619)
  return Array.from({ length: BARS }, () => {
    h = Math.imul(h ^ (h >>> 15), 2246822519) >>> 0
    return 0.25 + ((h % 1000) / 1000) * 0.75
  })
}

type Text = { state: 'idle' } | { state: 'loading' } | { state: 'shown'; text: string } | { state: 'hidden'; text: string }

type Props = { chat: string; id: string; durationS: number | null }

/** Voice note: play/pause, a waveform drawn from the duration, speed 1x/1.5x/2x and "Show text". */
export function VoiceNote({ chat, id, durationS }: Props) {
  const audio = useRef<HTMLAudioElement>(null)
  const [playing, setPlaying] = useState(false)
  const [current, setCurrent] = useState(0)
  const [total, setTotal] = useState(durationS)
  const [rate, setRate] = useState<(typeof RATES)[number]>(1)
  const [text, setText] = useState<Text>({ state: 'idle' })
  const [error, setError] = useState<string | null>(null)
  const bars = barHeights(id)
  const progress = total ? Math.min(1, current / total) : 0

  function toggle() {
    const el = audio.current
    if (!el) return
    if (el.paused) void el.play().catch(() => setPlaying(false))
    else el.pause()
  }

  function cycleRate() {
    const next = RATES[(RATES.indexOf(rate) + 1) % RATES.length]
    setRate(next)
    if (audio.current) audio.current.playbackRate = next
  }

  function seek(event: React.MouseEvent<HTMLDivElement>) {
    const el = audio.current
    if (!el || !total) return
    const box = event.currentTarget.getBoundingClientRect()
    el.currentTime = Math.max(0, Math.min(1, (event.clientX - box.left) / box.width)) * total
  }

  async function toggleText() {
    if (text.state === 'loading') return
    if (text.state === 'shown') return setText({ state: 'hidden', text: text.text })
    if (text.state === 'hidden') return setText({ state: 'shown', text: text.text })
    setError(null)
    setText({ state: 'loading' })
    try {
      const result = await apiFetch<{ text: string }>(
        `/api/transcripts/${encodeURIComponent(chat)}/${encodeURIComponent(id)}`,
        { method: 'POST' },
      )
      setText({ state: 'shown', text: result.text })
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Could not load the text')
      setText({ state: 'idle' })
    }
  }

  return (
    <div className="voice">
      <div className="vrow">
        <button
          type="button"
          className="vplay"
          onClick={toggle}
          aria-label={playing ? 'Pause voice message' : 'Play voice message'}
        >
          <Icon name={playing ? 'pause' : 'play'} size={22} />
        </button>
        <div className="wave" onClick={seek} aria-hidden="true">
          {bars.map((h, i) => (
            <span key={i} className={i / BARS < progress ? 'on' : ''} style={{ height: `${Math.round(h * 100)}%` }} />
          ))}
        </div>
        <span className="vtime">{formatDuration(Math.floor(playing || current > 0 ? current : (total ?? 0)))}</span>
        <button type="button" className="vrate" onClick={cycleRate} aria-label={`Playback speed ${rate}×`}>
          {rate}×
        </button>
        <audio
          ref={audio}
          src={mediaUrl(chat, id)}
          preload="none"
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => {
            setPlaying(false)
            setCurrent(0)
          }}
          onTimeUpdate={(e) => setCurrent(e.currentTarget.currentTime)}
          onLoadedMetadata={(e) => {
            const d = e.currentTarget.duration
            if (Number.isFinite(d) && d > 0) setTotal(Math.round(d))
            e.currentTarget.playbackRate = rate
          }}
        />
      </div>
      <button
        type="button"
        className="vtoggle"
        onClick={() => void toggleText()}
        aria-expanded={text.state === 'shown'}
        disabled={text.state === 'loading'}
      >
        {text.state === 'loading' && <span className="spin" role="status" aria-label="Loading text" />}
        {text.state === 'shown' ? 'Hide text' : 'Show text'}
      </button>
      {text.state === 'shown' && (
        <p className="vtext" dir="auto">
          {text.text || 'No speech found'}
        </p>
      )}
      {error && (
        <p className="verr" role="alert">
          {error}
        </p>
      )}
    </div>
  )
}
