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
  })
})
