import { AuthProvider } from 'react-admin'
import { config } from './config'

export const authProvider: AuthProvider = {
  login: async ({ username, password }) => {
    const response = await fetch(`${config.apiUrl}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: username, password })
    })

    if (!response.ok) {
      throw new Error('Invalid credentials')
    }

    const { user, token } = await response.json()
    localStorage.setItem('auth', JSON.stringify({ user, token }))
    return Promise.resolve()
  },

  logout: async () => {
    await fetch(`${config.apiUrl}/auth/logout`, { method: 'POST' })
    localStorage.removeItem('auth')
    return Promise.resolve()
  },

  checkAuth: async () => {
    return localStorage.getItem('auth') ? Promise.resolve() : Promise.reject()
  },

  checkError: async (error) => {
    if (error.status === 401 || error.status === 403) {
      localStorage.removeItem('auth')
      return Promise.reject()
    }
    return Promise.resolve()
  },

  getPermissions: async () => {
    const auth = localStorage.getItem('auth')
    if (!auth) return Promise.reject()

    const { user } = JSON.parse(auth)
    return Promise.resolve(user.role)
  },

  getIdentity: async () => {
    const auth = localStorage.getItem('auth')
    if (!auth) return Promise.reject()

    const { user } = JSON.parse(auth)
    return Promise.resolve({
      id: user.email,
      fullName: user.email,
      avatar: null
    })
  }
}
