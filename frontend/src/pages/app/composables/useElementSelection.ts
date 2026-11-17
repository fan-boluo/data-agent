import { ref, type Ref } from 'vue'
import { message } from 'ant-design-vue'
import { VisualEditor, type ElementInfo } from '@/utils/visualEditor'

/**
 * 网页元素选择/编辑模式状态（VisualEditor 联动）。
 * readOnlyTooltip 供输入框占位与权限提示复用。
 */
export function useElementSelection(canOperateApp: Ref<boolean>) {
  const readOnlyTooltip = '无法在别人的项目下操作哦~'

  const isEditMode = ref(false)
  const selectedElementInfo = ref<ElementInfo | null>(null)
  const visualEditor = new VisualEditor({
    onElementSelected: (elementInfo: ElementInfo) => {
      selectedElementInfo.value = elementInfo
    },
  })

  const toggleEditMode = () => {
    if (!canOperateApp.value) { message.warning(readOnlyTooltip); return }
    message.info('编辑模式需要网页预览，请先点击"预览网页"在新窗口打开后再编辑')
    // 以编辑器实际切换结果同步本地镜像，退出编辑模式时正确复位（无预览 iframe 时 enableEditMode 早退，镜像保持 false）
    isEditMode.value = visualEditor.toggleEditMode()
  }

  const clearSelectedElement = () => { selectedElementInfo.value = null; visualEditor.clearSelection() }

  return { readOnlyTooltip, isEditMode, selectedElementInfo, visualEditor, toggleEditMode, clearSelectedElement }
}
