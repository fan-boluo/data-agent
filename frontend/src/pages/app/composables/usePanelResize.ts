import { onBeforeUnmount, ref } from 'vue'

/**
 * IDE 面板拖拽调宽/调高：侧栏 ↔ 中间区、中间区 ↔ 聊天栏、编辑器 ↔ 输出区、输入区高度。
 * chatWidth 会被时间线域引用（切"分析链路"Tab 自动加宽）。
 */
export function usePanelResize() {
  // Resizable panels
  const sidePanelWidth = ref(260)
  const chatWidth = ref(320)
  const outputHeight = ref(150)
  const inputHeight = ref(100)
  const resizing = ref<'sidebar' | 'chat' | 'output' | 'input' | null>(null)

  const onResizeStart = (target: 'sidebar' | 'chat' | 'output' | 'input', e: MouseEvent) => {
    resizing.value = target
    document.addEventListener('mousemove', onResizeMove)
    document.addEventListener('mouseup', onResizeEnd)
    e.preventDefault()
  }

  const onResizeMove = (e: MouseEvent) => {
    if (resizing.value === 'sidebar') {
      const w = Math.max(200, Math.min(400, e.clientX))
      sidePanelWidth.value = w
    } else if (resizing.value === 'chat') {
      const w = Math.max(260, Math.min(600, window.innerWidth - e.clientX))
      chatWidth.value = w
    } else if (resizing.value === 'output') {
      const centerEl = document.querySelector('.center-column') as HTMLElement
      if (!centerEl) return
      const rect = centerEl.getBoundingClientRect()
      const h = Math.max(80, Math.min(rect.height * 0.5, rect.bottom - e.clientY))
      outputHeight.value = h
    } else if (resizing.value === 'input') {
      const chatPanel = document.querySelector('.right-chat-panel') as HTMLElement
      if (!chatPanel) return
      const rect = chatPanel.getBoundingClientRect()
      const h = Math.max(80, Math.min(300, rect.bottom - e.clientY))
      inputHeight.value = h
    }
  }

  const onResizeEnd = () => {
    resizing.value = null
    document.removeEventListener('mousemove', onResizeMove)
    document.removeEventListener('mouseup', onResizeEnd)
  }

  const disposeResizeListeners = () => {
    document.removeEventListener('mousemove', onResizeMove)
    document.removeEventListener('mouseup', onResizeEnd)
  }

  onBeforeUnmount(disposeResizeListeners)

  return { sidePanelWidth, chatWidth, outputHeight, inputHeight, resizing, onResizeStart, disposeResizeListeners }
}
