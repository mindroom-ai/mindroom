import { buildCinnyLoginUrl } from '../cinny'

describe('buildCinnyLoginUrl', () => {
  it('builds a MindRoom Chat login URL with the homeserver encoded in the route', () => {
    expect(buildCinnyLoginUrl('https://1.matrix.mindroom.chat')).toBe(
      'https://chat.mindroom.chat/login/https%3A%2F%2F1.matrix.mindroom.chat/'
    )
  })

  it('normalizes trailing slashes on the chat origin and homeserver URL', () => {
    expect(buildCinnyLoginUrl('https://1.matrix.mindroom.chat/', 'https://chat.mindroom.chat/')).toBe(
      'https://chat.mindroom.chat/login/https%3A%2F%2F1.matrix.mindroom.chat/'
    )
  })
})
