// 编译测试用的小前端：把常见的几个库都真用上，让打包器真去处理它们。
import { useMemo, useState } from 'react'
import { createBrowserRouter, RouterProvider, Link, useParams } from 'react-router'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { Button, Card, CardContent, Container, CssBaseline, Stack, TextField, Typography } from '@mui/material'
import { ThemeProvider, createTheme } from '@mui/material/styles'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'
import { groupBy, sortBy, debounce } from 'lodash-es'
import { addDays, format } from 'date-fns'
import { z } from 'zod'
import axios from 'axios'

const Item = z.object({ id: z.number(), name: z.string().min(1), score: z.number().min(0).max(100) })
type Item = z.infer<typeof Item>

const seed: Item[] = Array.from({ length: 60 }, (_, i) => Item.parse({ id: i, name: `item-${i}`, score: (i * 37) % 100 }))

function Chart({ items }: { items: Item[] }) {
  const data = useMemo(() => sortBy(items, 'id').map((x, i) => ({ day: format(addDays(new Date(2026, 0, 1), i), 'MM-dd'), score: x.score })), [items])
  return (
    <ResponsiveContainer width="100%" height={240}>
      <LineChart data={data}><XAxis dataKey="day" /><YAxis /><Tooltip /><Line dataKey="score" /></LineChart>
    </ResponsiveContainer>
  )
}

function Home() {
  const [q, setQ] = useState('')
  const onChange = useMemo(() => debounce((v: string) => setQ(v), 100), [])
  const { data } = useQuery({ queryKey: ['items'], queryFn: async () => { try { return (await axios.get<Item[]>('/api/items')).data } catch { return seed } } })
  const items = (data ?? seed).filter((x) => x.name.includes(q))
  const groups = groupBy(items, (x) => Math.floor(x.score / 25))
  return (
    <Container>
      <Typography variant="h4">berth 编译测试</Typography>
      <TextField label="筛选" onChange={(e) => onChange(e.target.value)} />
      <Chart items={items} />
      <Stack direction="row" spacing={1}>
        {Object.entries(groups).map(([k, v]) => (
          <Card key={k}><CardContent><Typography>{k}: {v.length}</Typography><Link to={`/item/${v[0].id}`}>看第一个</Link></CardContent></Card>
        ))}
      </Stack>
    </Container>
  )
}

function Detail() {
  const { id } = useParams()
  const item = seed.find((x) => String(x.id) === id)
  return <Container><Typography>{item ? `${item.name}: ${item.score}` : '没有'}</Typography><Button href="/">回去</Button></Container>
}

const router = createBrowserRouter([{ path: '/', element: <Home /> }, { path: '/item/:id', element: <Detail /> }])
const client = new QueryClient()

export default function App() {
  return (
    <ThemeProvider theme={createTheme()}>
      <CssBaseline />
      <QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>
    </ThemeProvider>
  )
}
