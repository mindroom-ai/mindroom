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

      // Check if user is an admin
      const { data: account } = await supabase
        .from('accounts')
        .select('is_admin')
        .eq('email', username)
        .single()

      if (!account?.is_admin) {
        await supabase.auth.signOut()
        throw new Error('Access denied. Admin privileges required.')
      }

      // Store the session
      if (data.session) {
        localStorage.setItem('auth_token', data.session.access_token)
        localStorage.setItem('refresh_token', data.session.refresh_token)
        localStorage.setItem('user', JSON.stringify(data.user))
        localStorage.setItem('is_admin', 'true')
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
    localStorage.removeItem('is_admin')
    return Promise.resolve()
  },

  checkAuth: async () => {
    const token = localStorage.getItem('auth_token')
    const isAdmin = localStorage.getItem('is_admin')

    if (!token || !isAdmin) {
      return Promise.reject()
    }

    // Verify the session is still valid
    const { data: { session }, error } = await supabase.auth.getSession()
    if (error || !session) {
      return Promise.reject()
    }

    // Double-check admin status in database
    const { data: account } = await supabase
      .from('accounts')
      .select('is_admin')
      .eq('email', session.user.email)
      .single()

    if (!account?.is_admin) {
      localStorage.removeItem('is_admin')
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
      localStorage.removeItem('is_admin')
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
