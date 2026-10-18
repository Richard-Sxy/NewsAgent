# 这边是 Python 的基础知识

## 基础知识

with 的作用：
    本质是进入和退出代码块的时候
        __enter__()
        __exit__()
    异步代码块
        __aenter__()
        __aexit__()
        异步代码块内部发生异常：
            向外抛出异常
            但是如果先发生退出，那么通常向外抛出 TimeoutError

代码参数的内容：
    def func(self, *, name: str):
        if type(name) is not str:
        这边要做一层校验，只是类型注解，不会强制检查内容

## 一些基础函数
lambda _: None  lambda表示一个匿名函数(接收一个参数) _参数名。下划线表示一种约定，这个参数我不用了，只是为了占位
        None 函数体表示函数体返回一个 None 值

arguments.model_dump()
        Pydantic模型对象转换成一个普通的 Python 字典
        class User(BaseModel):        # 只要继承了 BaseModel 对象就是 Pydantic 对象
            name: str
            age: int
        user = User(name="Alice", age=30)
        会得到 {'name': 'Alice', 'age': 30}
        转成 dict 以后，就会方便做序列化、传给其他库、存数据库、打印调试等等
    model_dump_json()  

## 异步编程
Callable 可调用对象 / Awaitable 可等待对象 / Coroutine 协程对象 / Future 未来执行结果的对象 / Task 负责驱动协程执行的对象
EventLoop 事件循环，负责调度

Awaitable 可等待对象
    只要一个对象实现了 __await__() 方法，并且这个方法返回符合要求的迭代对象，该对象就可以被 await 了。

    async def get_data():
        await asyncio.sleep(1)
        return {"success": True}
    async def main():
        result = get_data()
        print(type(result))
        print(await result)
    asyncio.run(main())

    这里 result = get_data() 得到的不是字典，是一个协程对象
    await result  协程才开始被驱动执行，最终得到结果

### 协程对象
    CPython 会在协程对象机器执行帧中保存执行状态：
        当前执行位置
        局部变量
        等待的对象
        协程是否正在运行等状态
    Task 如何驱动协程
        iterator.send(None) 用于向协程执行过程发送值并推进执行
        iterator.throw(exception) 用于向被暂停的执行过程注入异常
    Future 在这里起到什么作用
        Future 内部：
            状态 = PENDING
            结果 = 尚未设置
            异常 = 尚未设置
            完成回调 = []

## 基础注解
    @trace("conversation.turn",
        identity={
            "conversation_id": "newsagent.conversation_id",
            "claim.turn.id": "newsagent.turn_id"
        },
        result_status=True,
    )
    async def function(self, *, ...)
    装饰器，给 function() 的执行过程添加追踪记录方便排查问题、查看耗时和执行结果
    "conversation.turn" 给这次被追踪的操作起名，表示会话的一轮执行
    "conversation" 通常表示读取函数的 conversation_id 参数
    "claim.turn_id" 表示沿着 claim -> turn -> id 获取这一轮的ID
