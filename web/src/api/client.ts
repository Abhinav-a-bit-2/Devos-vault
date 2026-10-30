import type { components, paths } from './schema';
export type Entity = components['schemas']['EntityDetail'];
export type Summary = components['schemas']['EntitySummary'];
export type Page = components['schemas']['EntityPage'];
export type Graph = components['schemas']['GraphData'];
export type Impact = components['schemas']['ImpactData'];
export type Stats = components['schemas']['Overview'];
export type Job = components['schemas']['JobDetail'];
export type JobSummary = components['schemas']['JobSummary'];
export type NewEntity = components['schemas']['EntityInput'];
export type NewJob = Omit<components['schemas']['IngestInput'], 'offline' | 'replay'> & Partial<Pick<components['schemas']['IngestInput'], 'offline' | 'replay'>>;
type ApiPath = keyof paths;

async function request<T>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { method, signal, headers: method === 'GET' ? {} : { 'Content-Type': 'application/json', 'X-Devos-Request': '1' }, body: body === undefined ? undefined : JSON.stringify(body) });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error?.message || (Array.isArray(value.detail) ? value.detail.map((item: {msg: string}) => item.msg).join('; ') : 'The request could not be completed.'));
  return value as T;
}
function endpoint(path: ApiPath) { return path; }
export const api = {
  stats: () => request<Stats>(endpoint('/api/overview')),
  entities: (q = '', type = '', offset = 0, signal?: AbortSignal) => request<Page>(`${endpoint('/api/entities')}?q=${encodeURIComponent(q)}&limit=30&offset=${offset}${type ? `&type=${encodeURIComponent(type)}` : ''}`, 'GET', undefined, signal),
  entity: (id: string) => request<Entity>(`/api/entities/${encodeURIComponent(id)}`),
  add: (body: NewEntity) => request<Entity>(endpoint('/api/entities'), 'POST', body),
  edit: (id: string, body: components['schemas']['EntityEdit']) => request<Entity>(`/api/entities/${encodeURIComponent(id)}`, 'PATCH', body),
  link: (body: components['schemas']['LinkInput']) => request<components['schemas']['Relation']>(endpoint('/api/links'), 'POST', body),
  graph: (root = '', limit = 80) => request<Graph>(`${endpoint('/api/graph')}?limit=${limit}${root ? `&root=${encodeURIComponent(root)}` : ''}`),
  impact: (id: string, depth = 3) => request<Impact>(`/api/impact/${encodeURIComponent(id)}?depth=${depth}`),
  jobs: () => request<JobSummary[]>(endpoint('/api/jobs')),
  job: (id: string) => request<Job>(`/api/jobs/${encodeURIComponent(id)}`),
  ingest: (body: NewJob) => request<components['schemas']['JobAccepted']>(endpoint('/api/jobs'), 'POST', {offline:false,replay:false,...body}),
  cancel: (id: string) => request<Job>(`/api/jobs/${encodeURIComponent(id)}/cancel`, 'POST', {}),
  resume: (id: string) => request<Job>(`/api/jobs/${encodeURIComponent(id)}/resume`, 'POST', {}),
};
