import { Layout as RALayout, LayoutProps } from 'react-admin'
import { ReactQueryDevtools } from '@tanstack/react-query-devtools'

export const Layout = (props: LayoutProps) => (
  <>
    <RALayout {...props} />
    <ReactQueryDevtools initialIsOpen={false} />
  </>
)
