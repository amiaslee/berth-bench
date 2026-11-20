# 引擎起来、脚本跑到第一行就打印 READY 并退出：从起进程到退出，就是一次冷启动
extends SceneTree

func _init():
	print("READY")
	quit()
