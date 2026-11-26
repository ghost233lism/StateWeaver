import { useLocale } from '../../ui/locale'
import { label } from './model'

export function Status({ value }: { value: string }) {
  useLocale()

  return <span className={`studio-status studio-status--${value}`}><i aria-hidden="true" />{label(value)}</span>
}
