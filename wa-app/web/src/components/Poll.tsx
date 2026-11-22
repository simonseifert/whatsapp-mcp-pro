import type { PollView } from '../api-types'

/** Question, options with a bar sized by their share of the voters, and the voter count. Read-only. */
export function Poll({ poll }: { poll: PollView }) {
  const votes = poll.total === 1 ? '1 vote' : `${poll.total} votes`
  return (
    <div className="poll">
      <div className="pq" dir="auto">
        {poll.question}
      </div>
      {poll.options.map((option) => (
        <div className="opt" key={option.text}>
          <i style={{ width: `${poll.total ? Math.round((option.votes / poll.total) * 100) : 0}%` }} />
          <span dir="auto">{option.text}</span>
          <span>{option.votes}</span>
        </div>
      ))}
      {poll.options.length > 0 && <div className="v">{poll.total === 0 ? 'No votes yet' : votes}</div>}
    </div>
  )
}
