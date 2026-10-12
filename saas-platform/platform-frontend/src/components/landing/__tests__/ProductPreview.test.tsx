import { render, screen, within } from '@testing-library/react'
import { ProductPreview } from '../ProductPreview'

describe('ProductPreview', () => {
  it('describes the pictured MindRoom Chat room to screen readers', () => {
    render(<ProductPreview />)

    expect(screen.getByRole('img', { name: /Mind asks before booking a meeting/ })).toBeInTheDocument()
  })

  it('shows the approval Mind is waiting for', () => {
    render(<ProductPreview />)

    const preview = within(screen.getByRole('img', { name: /MindRoom Chat/ }))
    expect(preview.getByText('Approve')).toBeInTheDocument()
    expect(preview.getByText('Deny')).toBeInTheDocument()
    expect(preview.getByText('Pending approval')).toBeInTheDocument()
  })

  it('pictures only panels MindRoom Chat has: rooms, the thread, and the member list', () => {
    render(<ProductPreview />)

    const preview = within(screen.getByRole('img', { name: /MindRoom Chat/ }))
    expect(preview.getByText('Members')).toBeInTheDocument()
    expect(preview.queryByText('Run trace')).not.toBeInTheDocument()
    expect(preview.queryByText('Agents in this room')).not.toBeInTheDocument()
  })
})
