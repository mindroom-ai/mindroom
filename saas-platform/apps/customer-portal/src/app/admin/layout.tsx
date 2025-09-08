import { requireAdmin } from '@/lib/auth/admin'
import { AdminLayout } from '@/components/admin/AdminLayout'

export default async function AdminRootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  // This will redirect if not admin
  await requireAdmin()

  return <AdminLayout>{children}</AdminLayout>
}
