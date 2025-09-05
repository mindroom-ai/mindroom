import { AuthProvider } from 'react-admin'
import { createClient } from '@supabase/supabase-js'
import { config } from './config'

const supabase = createClient(config.supabaseUrl, config.supabaseServiceKey)

export const authProvider: AuthProvider = {
  login: async ({ username, password }) => {
    try {
      const { data, error } = await supabase.auth.signInWithPassword({
        email: username,
        password,
      })

      if (error) {
        throw new Error(error.message)
      }

      // Store the session
      if (data.session) {
        localStorage.setItem('auth_token', data.session.access_token)
        localStorage.setItem('refresh_token', data.session.refresh_token)
        localStorage.setItem('user', JSON.stringify(data.user))
      }

      return Promise.resolve()
    } catch (error) {
      return Promise.reject(error)
    }
  },

  logout: async () => {
    await supabase.auth.signOut()
    localStorage.removeItem('auth_token')
    localStorage.removeItem('refresh_token')
    localStorage.removeItem('user')
    return Promise.resolve()
  },

  checkAuth: async () => {
    const token = localStorage.getItem('auth_token')
    if (!token) {
      return Promise.reject()
    }

    // Verify the session is still valid
    const { data: { session }, error } = await supabase.auth.getSession()
    if (error || !session) {
      return Promise.reject()
    }

    return Promise.resolve()
  },

  checkError: (error) => {
    const status = error.status
    if (status === 401 || status === 403) {
      localStorage.removeItem('auth_token')
      localStorage.removeItem('refresh_token')
      localStorage.removeItem('user')
      return Promise.reject()
    }
    return Promise.resolve()
  },

  getPermissions: () => {
    // In a real app, you'd fetch roles/permissions from the server
    const user = localStorage.getItem('user')
    if (user) {
      const userData = JSON.parse(user)
      // Check if user is admin (you'd implement this based on your auth system)
      return Promise.resolve(userData.role || 'admin')
    }
    return Promise.reject()
  },

  getIdentity: () => {
    const user = localStorage.getItem('user')
    if (user) {
      const userData = JSON.parse(user)
      return Promise.resolve({
        id: userData.id,
        fullName: userData.email,
        avatar: null,
      })
    }
    return Promise.reject()
  },
}
