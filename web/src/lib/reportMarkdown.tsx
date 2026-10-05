/**
 * Markdown renderer for AI research reports.
 *
 * Dynamically imported (via React.lazy in reportCharts.tsx) so that
 * react-markdown / remark-gfm stay out of the main bundle chunk.
 * Consumed by the report tab rendering pipeline (B3-2).
 */

import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

export function MarkdownRenderer({ content }: { content: string }) {
  return (
    <div className="prose prose-sm max-w-none prose-headings:text-gray-800 prose-p:text-gray-700 prose-code:text-brand-700 prose-pre:bg-gray-900">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </div>
  )
}
