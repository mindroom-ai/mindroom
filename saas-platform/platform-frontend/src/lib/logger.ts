/**
 * Simple logger that removes console.log in production
 * KISS principle - no overengineering
 */

const isDevelopment = process.env.NODE_ENV === 'development'
const isTest = process.env.NODE_ENV === 'test'

export const logger = {
  log: (...args: any[]) => {
    if (isDevelopment || isTest) {
      console.log(...args)
    }
  },

  error: (...args: any[]) => {
    if (isDevelopment || isTest) {
      console.error(...args)
    }
    // In production, you might want to send to error tracking service
    // but never log sensitive data
  },

  warn: (...args: any[]) => {
    if (isDevelopment || isTest) {
      console.warn(...args)
    }
  }
}
