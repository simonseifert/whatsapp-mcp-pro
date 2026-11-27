import { useCallback, useEffect, useRef, useState } from 'react'

export const MAX_SECONDS = 15 * 60
const TYPES = ['audio/webm;codecs=opus', 'audio/mp4'] as const

export type RecorderState = 'idle' | 'asking' | 'recording' | 'stopped' | 'denied'

/** True where the browser can record audio at all (secure page, MediaRecorder and a microphone API). */
export const canRecord = () =>
  typeof MediaRecorder !== 'undefined' && typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia

/** The file name for a recorded blob: iOS gives audio/mp4 (m4a), Chrome and Firefox give webm. */
export const voiceFilename = (blob: Blob) => (blob.type.startsWith('audio/mp4') ? 'voice-note.m4a' : 'voice-note.webm')

export const DENIED_MESSAGE = 'Microphone access is off. Allow it in Settings to record.'
const NO_MIC_MESSAGE = 'No microphone found.'
const FAILED_MESSAGE = "Couldn't start the microphone."

const messageFor = (err: unknown) => {
  const name = err instanceof DOMException || err instanceof Error ? err.name : ''
  if (name === 'NotAllowedError' || name === 'SecurityError') return DENIED_MESSAGE
  if (name === 'NotFoundError') return NO_MIC_MESSAGE
  return FAILED_MESSAGE
}

const stopTracks = (media: MediaStream | null) => media?.getTracks().forEach((t) => t.stop())

type Session = { rec: MediaRecorder; media: MediaStream; discarded: boolean }

/**
 * Records one voice note with MediaRecorder. Nothing leaves the device here: `blob` is the result after `stop()`.
 * The microphone is released on stop, cancel, unmount and when the page is hidden mid-recording (that counts as stop).
 * `state` 'denied' also covers other microphone failures; `message` says which.
 */
export function useRecorder() {
  const [state, setState] = useState<RecorderState>('idle')
  const [message, setMessage] = useState<string | null>(null)
  const [seconds, setSeconds] = useState(0)
  const [blob, setBlob] = useState<Blob | null>(null)
  const session = useRef<Session | null>(null)
  const generation = useRef(0) // cancel and unmount bump it: a getUserMedia answer from before is ignored
  const asking = useRef(false)
  const chunks = useRef<Blob[]>([])
  const timer = useRef<ReturnType<typeof setInterval> | null>(null)
  const ticks = useRef(0)

  const clearTimer = useCallback(() => {
    if (timer.current) clearInterval(timer.current)
    timer.current = null
  }, [])

  const stop = useCallback(() => {
    const cur = session.current
    if (!cur || cur.rec.state === 'inactive') return
    clearTimer()
    cur.rec.stop() // the 'stop' event publishes the blob
    stopTracks(cur.media) // the microphone is free right now, not when the event arrives
  }, [clearTimer])

  const discard = useCallback(() => {
    const cur = session.current
    session.current = null
    clearTimer()
    if (cur) {
      cur.discarded = true
      if (cur.rec.state !== 'inactive') cur.rec.stop()
      stopTracks(cur.media)
    }
  }, [clearTimer])

  const cancel = useCallback(() => {
    generation.current += 1
    asking.current = false
    discard()
    chunks.current = []
    setBlob(null)
    setSeconds(0)
    setMessage(null)
    setState('idle')
  }, [discard])

  const start = useCallback(async () => {
    if (session.current || asking.current) return
    const mine = ++generation.current
    asking.current = true
    setMessage(null)
    setState('asking')
    let media: MediaStream
    try {
      media = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch (err) {
      if (mine !== generation.current) return
      asking.current = false
      setMessage(messageFor(err))
      setState('denied')
      return
    }
    if (mine !== generation.current) {
      stopTracks(media) // cancelled or unmounted while the prompt was open
      return
    }
    asking.current = false
    const type = TYPES.find((t) => MediaRecorder.isTypeSupported(t))
    let rec: MediaRecorder
    try {
      rec = new MediaRecorder(media, type ? { mimeType: type } : undefined)
      const cur: Session = { rec, media, discarded: false }
      chunks.current = []
      rec.addEventListener('dataavailable', (e) => {
        const data = (e as BlobEvent).data
        if (!cur.discarded && data && data.size > 0) chunks.current.push(data)
      })
      rec.addEventListener('stop', () => {
        // A late event from a cancelled or replaced recorder must not touch the current one.
        if (cur.discarded || session.current !== cur) return
        stopTracks(media)
        setBlob(new Blob(chunks.current, { type: rec.mimeType || type || 'audio/webm' }))
        setState('stopped')
      })
      session.current = cur
      rec.start()
    } catch {
      session.current = null
      stopTracks(media)
      setMessage(FAILED_MESSAGE)
      setState('idle')
      return
    }
    ticks.current = 0
    setBlob(null)
    setSeconds(0)
    setState('recording')
    timer.current = setInterval(() => {
      ticks.current += 1
      setSeconds(ticks.current)
      if (ticks.current >= MAX_SECONDS) stop()
    }, 1000)
  }, [stop])

  // A hidden page (locked phone, app switch) ends the recording but keeps what was said.
  useEffect(() => {
    if (state !== 'recording') return
    const onHide = () => {
      if (document.hidden) stop()
    }
    document.addEventListener('visibilitychange', onHide)
    return () => document.removeEventListener('visibilitychange', onHide)
  }, [state, stop])

  useEffect(
    () => () => {
      generation.current += 1
      discard()
    },
    [discard],
  )

  return { state, message, seconds, blob, start, stop, cancel }
}
