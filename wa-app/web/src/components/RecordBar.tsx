import { useEffect, useRef, useState } from 'react'
import { formatDuration } from '../format'
import { DENIED_MESSAGE, type RecorderState } from '../useRecorder'
import { Icon } from './Icon'

type Props = {
  state: RecorderState
  seconds: number
  blob: Blob | null
  /** The line shown when the microphone could not be used. */
  message?: string | null
  onStop: () => void
  onCancel: () => void
  onSend: (blob: Blob) => void
}

/** Replaces the message box while a voice note is recorded: red dot and timer, then a preview with Send. */
export function RecordBar({ state, seconds, blob, message, onStop, onCancel, onSend }: Props) {
  const audio = useRef<HTMLAudioElement>(null)
  const [playing, setPlaying] = useState(false)

  // The preview plays a local object URL; it is set on the element directly and revoked when the blob goes.
  useEffect(() => {
    const el = audio.current
    if (!blob || !el || typeof URL.createObjectURL !== 'function') return
    const made = URL.createObjectURL(blob)
    el.src = made
    return () => URL.revokeObjectURL(made)
  }, [blob])

  function toggle() {
    const el = audio.current
    if (!el) return
    if (el.paused) void el.play().catch(() => setPlaying(false))
    else el.pause()
  }

  if (state === 'denied') {
    return (
      <div className="recordbar">
        <p className="record-denied" role="alert">
          {message ?? DENIED_MESSAGE}
        </p>
        <button className="iconbtn" type="button" aria-label="Close" onClick={onCancel}>
          <Icon name="close" size={22} />
        </button>
      </div>
    )
  }

  const time = formatDuration(seconds)
  if (state === 'stopped' && blob) {
    return (
      <div className="recordbar">
        <button className="iconbtn" type="button" aria-label="Delete recording" onClick={onCancel}>
          <Icon name="close" size={22} />
        </button>
        <button className="iconbtn" type="button" aria-label={playing ? 'Pause' : 'Play'} onClick={toggle}>
          <Icon name={playing ? 'pause' : 'play'} size={22} />
        </button>
        <span className="record-time" aria-label="Length">
          {time}
        </span>
        <audio
          ref={audio}
          preload="metadata"
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => setPlaying(false)}
        />
        <span className="record-fill" />
        <button className="sendbtn" type="button" aria-label="Send voice message" onClick={() => onSend(blob)}>
          <Icon name="send" size={22} />
        </button>
      </div>
    )
  }

  return (
    <div className="recordbar">
      <button className="iconbtn" type="button" aria-label="Delete recording" onClick={onCancel}>
        <Icon name="close" size={22} />
      </button>
      <span className="record-dot" aria-hidden="true" />
      <span className="record-time" role="timer" aria-label="Recording time">
        {time}
      </span>
      <span className="record-fill" />
      <button className="sendbtn" type="button" aria-label="Stop recording" disabled={state !== 'recording'} onClick={onStop}>
        <span className="record-stop" aria-hidden="true" />
      </button>
    </div>
  )
}
