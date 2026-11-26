import { useLocale } from '../../ui/locale'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { EntityLink, useEntityLinks } from './EntityLinks'
import { remarkEntityLinks } from './entity-link-model'
import './Markdown.css'

// Keep link and image elements mounted when streamed prose or scoped data updates.
const components: Components = {
  a: function EntityAnchor({ href, children, title, node }) {
  useLocale()

    const entities = useEntityLinks()
    const id = node?.properties['data-studio-entity']
    return entities && typeof id === 'string' ? <EntityLink id={id} />
      : href ? <a href={href} title={title} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>
  },
  img: ({ src, alt, title }) => src ? <img src={src} alt={alt ?? ''} title={title} loading="lazy" referrerPolicy="no-referrer" /> : <span>{alt}</span>,
  table: ({ children }) => <div className="studio-markdown-table"><table>{children}</table></div>,
}

/** Original prose is parsed directly, including incomplete streaming Markdown. */
export function Markdown({ children, className = '' }: { children: string; className?: string }) {
  useLocale()

  const entities = useEntityLinks()
  return <div className={`studio-markdown ${className}`}><ReactMarkdown remarkPlugins={entities ? [remarkGfm, [remarkEntityLinks, { project: entities.project, source: children }]] : [remarkGfm]} urlTransform={safeUrl} components={components}>{children}</ReactMarkdown></div>
}

function safeUrl(value: string) {
  // React never receives an executable scheme, including whitespace-obfuscated ones.
  const compact = value.replace(/%(?:0[\da-f]|1[\da-f]|20|7f)/gi, '').replace(/[\u0000-\u0020\u007f]/g, '')
  const scheme = compact.match(/^([a-z][a-z\d+.-]*):/i)?.[1].toLowerCase()
  return scheme && !['https', 'http', 'mailto'].includes(scheme) ? '' : value
}
