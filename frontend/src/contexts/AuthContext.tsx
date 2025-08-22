import React, { createContext, useContext, useState, useEffect, ReactNode } from 'react';
import axios from 'axios';

interface AuthContextType {
  isAuthenticated: boolean;
  isAuthEnabled: boolean;
  username: string | null;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  checkAuth: () => Promise<void>;
  loading: boolean;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

// Configure axios defaults
const API_BASE_URL = (import.meta as any).env?.VITE_API_URL || 'http://localhost:8765';
axios.defaults.baseURL = API_BASE_URL;
axios.defaults.withCredentials = true;

// Add interceptor to include session token from localStorage if cookies don't work
axios.interceptors.request.use((config: any) => {
  const token = localStorage.getItem('session_token');
  if (token && config.headers) {
    config.headers['X-Session-Token'] = token;
  }
  return config;
});

export const AuthProvider: React.FC<{ children: ReactNode }> = ({ children }) => {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isAuthEnabled, setIsAuthEnabled] = useState(false);
  const [username, setUsername] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const checkAuth = async () => {
    try {
      const response = await axios.get('/api/auth/status');
      setIsAuthEnabled(response.data.enabled);
      setIsAuthenticated(response.data.authenticated);
      setUsername(response.data.username);

      // If auth is disabled, consider user authenticated
      if (!response.data.enabled) {
        setIsAuthenticated(true);
      }
    } catch (error) {
      console.error('Auth check failed:', error);
      setIsAuthEnabled(false);
      setIsAuthenticated(false);
      setUsername(null);
    } finally {
      setLoading(false);
    }
  };

  const login = async (username: string, password: string) => {
    try {
      const response = await axios.post('/api/auth/login', { username, password });
      if (response.data.success) {
        // Store token in localStorage as backup
        if (response.data.session_token) {
          localStorage.setItem('session_token', response.data.session_token);
        }
        setIsAuthenticated(true);
        setUsername(username);
      }
    } catch (error: any) {
      console.error('Login failed:', error);
      throw new Error(error.response?.data?.detail || 'Login failed');
    }
  };

  const logout = async () => {
    try {
      await axios.post('/api/auth/logout');
    } catch (error) {
      console.error('Logout failed:', error);
    } finally {
      localStorage.removeItem('session_token');
      setIsAuthenticated(false);
      setUsername(null);
    }
  };

  useEffect(() => {
    checkAuth();
  }, []);

  return (
    <AuthContext.Provider
      value={{
        isAuthenticated,
        isAuthEnabled,
        username,
        login,
        logout,
        checkAuth,
        loading,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};
