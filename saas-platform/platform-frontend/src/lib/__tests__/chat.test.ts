import { buildChatLoginUrl } from '../chat'

describe('buildChatLoginUrl', () => {
  it('builds a MindRoom Chat login URL with the homeserver encoded in the route', () => {
    expect(buildChatLoginUrl('https://1.matrix.mindroom.chat')).toBe(
      'https://chat.mindroom.chat/login/https%3A%2F%2F1.matrix.mindroom.chat/'
    )
  })

  it('normalizes trailing slashes on the chat origin and homeserver URL', () => {
    expect(buildChatLoginUrl('https://1.matrix.mindroom.chat/', 'https://chat.mindroom.chat/')).toBe(
      'https://chat.mindroom.chat/login/https%3A%2F%2F1.matrix.mindroom.chat/'
    )
  })
})
