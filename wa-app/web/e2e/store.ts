import { DatabaseSync } from 'node:sqlite'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

const port = process.env.WA_APP_PORT ?? '18085'
// e2e/serve.sh builds the fictional store here; the server reads it read-only, the bridge is simulated by these writes.
const dbPath = join(process.env.TMPDIR ?? tmpdir(), `wa-app-e2e-${port}`, 'store', 'messages.db')

export const LUKAS = '4915100000005@s.whatsapp.net'
export const MARTA = '385910000015@s.whatsapp.net'

/** "YYYY-MM-DD HH:MM:SS+00:00", `minutesAgo` before now (the format the bridge stores). */
export function stamp(minutesAgo = 0): string {
  return new Date(Date.now() - minutesAgo * 60_000).toISOString().slice(0, 19).replace('T', ' ') + '+00:00'
}

/** Plays the bridge: inserts messages into the fictional store, and takes them out again so specs stay independent. */
export class FakeBridge {
  private added: [string, string][] = []

  private run<T>(work: (db: DatabaseSync) => T): T {
    const db = new DatabaseSync(dbPath)
    try {
      db.exec('PRAGMA busy_timeout = 5000; PRAGMA foreign_keys = OFF')
      return work(db)
    } finally {
      db.close()
    }
  }

  insert(chat: string, name: string, text: string, minutesAgo = 0): void {
    const id = `${name}-${Date.now().toString(36)}` // unique per run: moved rows keep their ids
    this.run((db) =>
      db
        .prepare(
          'INSERT OR REPLACE INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?,?,?,?,?,0)',
        )
        .run(id, chat, chat, text, stamp(minutesAgo)),
    )
    this.added.push([chat, id])
  }

  /** Adds `count` fictional filler chats (one message each), all older than every fixture chat, `Filler 000` newest. */
  insertFillerChats(count: number): void {
    const run = Date.now().toString(36) // unique per run: moved rows keep their ids
    this.run((db) => {
      const chat = db.prepare('INSERT OR REPLACE INTO chats (jid, name, last_message_time) VALUES (?,?,?)')
      const message = db.prepare(
        'INSERT OR REPLACE INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES (?,?,?,?,?,0)',
      )
      for (let i = 0; i < count; i++) {
        const jid = `3859700${String(i).padStart(5, '0')}@s.whatsapp.net`
        const at = stamp(60 * 24 * 30 + i) // a month back, one minute apart
        chat.run(jid, `Filler ${String(i).padStart(3, '0')}`, at)
        message.run(`FILLER${run}-${i}`, jid, jid, `filler ${i}`, at)
        this.added.push([jid, `FILLER${run}-${i}`])
      }
    })
  }

  /** Takes the inserted messages out of their chats. Rows are moved, not deleted: SQLite would hand a deleted newest
   *  rowid to the next insert, which no real bridge write does. */
  cleanup(): void {
    this.run((db) => {
      for (const [chat, id] of this.added)
        db.prepare("UPDATE messages SET chat_jid = 'removed@e2e' WHERE chat_jid = ? AND id = ?").run(chat, id)
    })
    this.added = []
  }
}

const appDbPath = join(process.env.TMPDIR ?? tmpdir(), `wa-app-e2e-${port}`, 'data', 'app.db')

/** Forgets the app's own read marker for a chat, so a spec that read it leaves the fixtures as it found them. */
export function forgetAppRead(chat: string): void {
  const db = new DatabaseSync(appDbPath)
  try {
    db.exec('PRAGMA busy_timeout = 5000')
    db.prepare('DELETE FROM read_state WHERE chat_jid = ?').run(chat)
  } finally {
    db.close()
  }
}

const stubPortFile = join(process.env.TMPDIR ?? tmpdir(), `wa-app-e2e-${port}`, 'stub.port')

export type StubCall = {
  path: string
  recipient?: string
  message?: string
  quoted_message_id?: string | null
  idempotency_key?: string
  message_id?: string
  emoji?: string
}

/** The stand-in bridge that e2e/serve.sh starts (e2e/stub_bridge.py): what it was asked, and what it should do next. */
export const stub = {
  async call(path: string, body?: object): Promise<unknown> {
    const base = `http://127.0.0.1:${readFileSync(stubPortFile, 'utf8').trim()}`
    const response = await fetch(
      base + path,
      body === undefined ? undefined : { method: 'POST', body: JSON.stringify(body) },
    )
    return response.json()
  },
  log: async () => (await stub.call('/__stub/log')) as StubCall[],
  reset: () => stub.call('/__stub/reset', {}),
  failNext: (status: number, code: string, error = 'refused') =>
    stub.call('/__stub/fail', { status, code, error, count: 1 }),
  delay: (seconds: number) => stub.call('/__stub/delay', { seconds }),
  /** Takes the messages the stub "sent" out of their chats again (moved, like FakeBridge.cleanup). */
  cleanup(): void {
    const db = new DatabaseSync(dbPath)
    try {
      db.exec('PRAGMA busy_timeout = 5000; PRAGMA foreign_keys = OFF')
      db.exec("UPDATE messages SET chat_jid = 'removed@e2e' WHERE id LIKE 'STUB%'")
    } finally {
      db.close()
    }
  },
}
