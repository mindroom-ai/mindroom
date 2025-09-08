import { DataProvider } from 'react-admin'
import { config } from './config'

// Helper to build query string
const buildQueryString = (params: any) => {
  const query = new URLSearchParams()
  Object.keys(params).forEach(key => {
    if (params[key] !== undefined && params[key] !== null) {
      query.append(key, params[key])
    }
  })
  return query.toString()
}

export const dataProvider: DataProvider = {
  getList: async (resource, params) => {
    const { page, perPage } = params.pagination
    const { field, order } = params.sort
    const filter = params.filter || {}

    const query = buildQueryString({
      _sort: field,
      _order: order,
      _start: (page - 1) * perPage,
      _end: page * perPage,
      ...filter
    })

    const response = await fetch(`${config.apiUrl}/${resource}?${query}`)
    if (!response.ok) throw new Error(response.statusText)

    const json = await response.json()
    return {
      data: json.data,
      total: json.total
    }
  },

  getOne: async (resource, params) => {
    const response = await fetch(`${config.apiUrl}/${resource}/${params.id}`)
    if (!response.ok) throw new Error(response.statusText)

    const json = await response.json()
    return json
  },

  getMany: async (resource, params) => {
    const promises = params.ids.map(id =>
      fetch(`${config.apiUrl}/${resource}/${id}`).then(r => r.json())
    )
    const responses = await Promise.all(promises)
    return { data: responses.map(r => r.data) }
  },

  getManyReference: async (resource, params) => {
    const { page, perPage } = params.pagination
    const { field, order } = params.sort

    const query = buildQueryString({
      [params.target]: params.id,
      _sort: field,
      _order: order,
      _start: (page - 1) * perPage,
      _end: page * perPage,
      ...params.filter
    })

    const response = await fetch(`${config.apiUrl}/${resource}?${query}`)
    if (!response.ok) throw new Error(response.statusText)

    const json = await response.json()
    return {
      data: json.data,
      total: json.total
    }
  },

  create: async (resource, params) => {
    const response = await fetch(`${config.apiUrl}/${resource}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params.data)
    })
    if (!response.ok) throw new Error(response.statusText)

    const json = await response.json()
    return json
  },

  update: async (resource, params) => {
    const response = await fetch(`${config.apiUrl}/${resource}/${params.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params.data)
    })
    if (!response.ok) throw new Error(response.statusText)

    const json = await response.json()
    return json
  },

  updateMany: async (resource, params) => {
    const promises = params.ids.map(id =>
      fetch(`${config.apiUrl}/${resource}/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params.data)
      }).then(r => r.json())
    )
    const responses = await Promise.all(promises)
    return { data: responses.map(r => r.data) }
  },

  delete: async (resource, params) => {
    const response = await fetch(`${config.apiUrl}/${resource}/${params.id}`, {
      method: 'DELETE'
    })
    if (!response.ok) throw new Error(response.statusText)

    const json = await response.json()
    return json
  },

  deleteMany: async (resource, params) => {
    const promises = params.ids.map(id =>
      fetch(`${config.apiUrl}/${resource}/${id}`, {
        method: 'DELETE'
      }).then(r => r.json())
    )
    await Promise.all(promises)
    return { data: params.ids }
  }
}
