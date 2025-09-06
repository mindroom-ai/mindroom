import { DataProvider } from 'react-admin'
import { createClient } from '@supabase/supabase-js'
import { config } from './config'

const supabase = createClient(config.supabaseUrl, config.supabaseServiceKey)

export const dataProvider: DataProvider = {
  getList: async (resource, params) => {
    const { page, perPage } = params.pagination
    const { field, order } = params.sort
    const filter = params.filter || {}

    // Build query
    let query = supabase
      .from(resource)
      .select('*', { count: 'exact' })

    // Apply filters
    Object.keys(filter).forEach(key => {
      if (filter[key] !== undefined && filter[key] !== '') {
        // Handle special filter operators
        if (key === 'q') {
          // Search filter
          query = query.or(`email.ilike.%${filter[key]}%,full_name.ilike.%${filter[key]}%,company_name.ilike.%${filter[key]}%`)
        } else if (typeof filter[key] === 'object' && filter[key] !== null) {
          // Range filters
          if ('gte' in filter[key]) {
            query = query.gte(key, filter[key].gte)
          }
          if ('lte' in filter[key]) {
            query = query.lte(key, filter[key].lte)
          }
        } else {
          query = query.eq(key, filter[key])
        }
      }
    })

    // Apply sorting
    if (field) {
      query = query.order(field, { ascending: order === 'ASC' })
    }

    // Apply pagination
    const start = (page - 1) * perPage
    const end = start + perPage - 1
    query = query.range(start, end)

    const { data, count, error } = await query

    if (error) throw error

    return {
      data: data || [],
      total: count || 0,
    }
  },

  getOne: async (resource, params) => {
    const { data, error } = await supabase
      .from(resource)
      .select('*')
      .eq('id', params.id)
      .single()

    if (error) throw error

    return { data }
  },

  getMany: async (resource, params) => {
    const { data, error } = await supabase
      .from(resource)
      .select('*')
      .in('id', params.ids)

    if (error) throw error

    return { data: data || [] }
  },

  getManyReference: async (resource, params) => {
    const { page, perPage } = params.pagination
    const { field, order } = params.sort

    let query = supabase
      .from(resource)
      .select('*', { count: 'exact' })
      .eq(params.target, params.id)

    if (field) {
      query = query.order(field, { ascending: order === 'ASC' })
    }

    const start = (page - 1) * perPage
    const end = start + perPage - 1
    query = query.range(start, end)

    const { data, count, error } = await query

    if (error) throw error

    return {
      data: data || [],
      total: count || 0,
    }
  },

  create: async (resource, params) => {
    const { data, error } = await supabase
      .from(resource)
      .insert(params.data)
      .select()
      .single()

    if (error) throw error

    return { data }
  },

  update: async (resource, params) => {
    const { data, error } = await supabase
      .from(resource)
      .update(params.data)
      .eq('id', params.id)
      .select()
      .single()

    if (error) throw error

    return { data }
  },

  updateMany: async (resource, params) => {
    const { data, error } = await supabase
      .from(resource)
      .update(params.data)
      .in('id', params.ids)
      .select()

    if (error) throw error

    return { data: params.ids }
  },

  delete: async (resource, params) => {
    const { data, error } = await supabase
      .from(resource)
      .delete()
      .eq('id', params.id)
      .select()
      .single()

    if (error) throw error

    return { data }
  },

  deleteMany: async (resource, params) => {
    const { error } = await supabase
      .from(resource)
      .delete()
      .in('id', params.ids)

    if (error) throw error

    return { data: params.ids }
  },
}
