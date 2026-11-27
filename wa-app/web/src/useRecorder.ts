import { useCallback, useEffect, useRef, useState } from 'react'

export const MAX_SECONDS = 15 * 60
const TYPES = ['audio/webm;codecs=opus', 'audio/mp4'] as const

export type RecorderState = 'idle' | 'asking' | 'recording' | 'stopped' | 'denied'

/** True where the browser can record audio at all (secure page, MediaRecorder and a microphone API). */
export const canRecord = () =>
  typeof MediaRecorder !== 'undefined' && typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia

/** The file name for a recorded blob: iOS gives audio/mp4 (m4a), Chrome and Firefox give webm. */
export const voiceFilename = (blob: Blob) => (blob.type.startsWith('audio/mp4') ? 'voice-note.m4a' : 'voice-note.webm')

/**
 * Records one voice note with MediaRecorder. Nothing leaves the device here: `blob` is the result after `stop()`.
 * The microphone is released on stop, cancel, unmount and when the page is hidden mid-recording (that counts as stop).
 */
export function useRecorder() {
  const [state, setState] = useState<RecorderState>('idle')
  const [seconds, setSeconds] = useState(0)
  const [blob, setBlob] = useState<Blob | null>(null)
  const recorder = useRef<MediaRecorder | null>(null)
  const stream = useRef<MediaStream | null>(null)
  const chunks = useRef<Blob[]>([])
  const timer = useRef<ReturnType<typeof setInterval> | null>(null)
  const ticks = useRef(0)
  const discard = useRef(false)

  const release = useCallback(() => {
    if (timer.current) clearInterval(timer.current)
    timer.current = null
    stream.current?.getTracks().forEach((t) => t.stop())
    stream.current = null
  }, [])

  const stop = useCallback(() => {
    const rec = recorder.current
    if (rec && rec.state !== 'inactive') rec.stop() // the 'stop' event finishes the job
  }, [])

  const cancel = useCallback(() => {
    discard.current = true
    const rec = recorder.current
    recorder.current = null
    if (rec && rec.state !== 'inactive') rec.stop()
    release()
    chunks.current = []
    setBlob(null)
    setSeconds(0)
    setState('idle')
  }, [release])

  const start = useCallback(async () => {
    if (recorder.current) return
    setState('asking')
    let media: MediaStream
    try {
      media = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch {
      setState('denied')
      return
    }
    const type = TYPES.find((t) => MediaRecorder.isTypeSupported(t))
    const rec = new MediaRecorder(media, type ? { mimeType: type } : undefined)
    stream.current = media
    recorder.current = rec
    chunks.current = []
    discard.current = false
    ticks.current = 0
    setBlob(null)
    setSeconds(0)
    rec.addEventListener('dataavailable', (e) => {
      const data = (e as BlobEvent).data
      if (data && data.size > 0) chunks.current.push(data)
    })
    rec.addEventListener('stop', () => {
      release()
      if (discard.current) return
      recorder.current = null
      setBlob(new Blob(chunks.current, { type: rec.mimeType || type || 'audio/webm' }))
      setState('stopped')
    })
    rec.start()
    setState('recording')
    timer.current = setInterval(() => {
      ticks.current += 1
      setSeconds(ticks.current)
      if (ticks.current >= MAX_SECONDS) stop()
    }, 1000)
  }, [release, stop])

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
      discard.current = true
      const rec = recorder.current
      if (rec && rec.state !== 'inactive') rec.stop()
      release()
    },
    [release],
  )

  return { state, seconds, blob, start, stop, cancel }
}
