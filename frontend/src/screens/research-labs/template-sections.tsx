/**
 * Template Lab's two ways to research a template. Advanced is the typed expression editor and
 * opens first; Basic is the classic block builder. Each is a screen of its own, so a link, a
 * reload or the Data Explorer's round trip lands back on the one that asked.
 */

import { TabBar, TabLink } from '@/ui/kit'

export function TemplateSections() {
  return (
    <TabBar>
      <TabLink to="/labs/template" activeOptions={{ exact: true }}>
        Advanced Template Research
      </TabLink>
      <TabLink to="/labs/template/basic">Basic Template Research</TabLink>
    </TabBar>
  )
}
