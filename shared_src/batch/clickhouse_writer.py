"""
批量写入 ClickHouse
支持缓冲区和定时刷新
"""
import threading
import time
from typing import Dict, Any, List, Optional, Callable
import logging
import os

logger = logging.getLogger(__name__)


class BatchClickHouseWriter:
    """
    批量写入 ClickHouse
    
    特性：
    - 累积到 batch_size 或超过 flush_interval 时刷新
    - 线程安全
    - 支持后台定时刷新
    """
    
    def __init__(
        self,
        client,
        table: str,
        batch_size: Optional[int] = None,
        flush_interval: Optional[float] = None,
        columns: Optional[str] = None
    ):
        """
        初始化批量写入器

        Args:
            client: ClickHouse 客户端
            table: 目标表名
            batch_size: 批量大小（默认从环境变量读取）
            flush_interval: 刷新间隔秒数（默认从环境变量读取）
            columns: 列名列表（用于 INSERT 语句）
        """
        self.client = client
        self.table = table
        self.batch_size = batch_size or int(os.getenv("CH_BATCH_SIZE", "2000"))
        self.flush_interval = flush_interval or float(os.getenv("CH_FLUSH_INTERVAL", "0.5"))
        self.columns = columns
        
        self._buffer: List[List] = []
        self._lock = threading.Lock()
        self._last_flush = time.time()
        self._flush_thread: Optional[threading.Thread] = None
        self._running = False
        self._stats = {
            "total_rows": 0,
            "total_flushes": 0,
            "total_errors": 0
        }
    
    def start(self):
        """启动后台刷新线程"""
        if self._running:
            return
        
        self._running = True
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()
        logger.info(f"BatchClickHouseWriter started for table {self.table}")
    
    def stop(self):
        """停止并刷新剩余数据"""
        self._running = False
        self.flush()
        logger.info(f"BatchClickHouseWriter stopped for table {self.table}, stats: {self._stats}")
    
    def add(self, row: List) -> bool:
        """
        添加一行数据到缓冲区

        Args:
            row: 数据行（列表格式）

        Returns:
            bool: 是否触发了刷新
        """
        with self._lock:
            self._buffer.append(row)
            self._stats["total_rows"] += 1
            
            if len(self._buffer) >= self.batch_size:
                return self._do_flush()
        return False
    
    def add_batch(self, rows: List[List]) -> bool:
        """
        批量添加数据

        Args:
            rows: 数据行列表

        Returns:
            bool: 是否触发了刷新
        """
        with self._lock:
            self._buffer.extend(rows)
            self._stats["total_rows"] += len(rows)
            
            if len(self._buffer) >= self.batch_size:
                return self._do_flush()
        return False
    
    def flush(self) -> bool:
        """强制刷新缓冲区"""
        with self._lock:
            return self._do_flush()
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        with self._lock:
            return {
                **self._stats,
                "buffer_size": len(self._buffer),
                "batch_size": self.batch_size,
                "flush_interval": self.flush_interval
            }
    
    def _do_flush(self) -> bool:
        """
        执行刷新（需要在锁内调用）

        Returns:
            bool: 是否成功
        """
        if not self._buffer:
            return True
        
        buffer_to_flush = self._buffer
        self._buffer = []
        
        try:
            if self.columns:
                query = f'INSERT INTO {self.table} ({self.columns}) VALUES'
            else:
                query = f'INSERT INTO {self.table} VALUES'
            
            logger.info(f"Flushing {len(buffer_to_flush)} rows to {self.table}")
            self.client.execute(query, buffer_to_flush)
            
            self._stats["total_flushes"] += 1
            self._last_flush = time.time()
            
            logger.info(f"Successfully flushed {len(buffer_to_flush)} rows to {self.table}")
            return True
            
        except Exception as e:
            self._stats["total_errors"] += 1
            logger.error(f"Failed to flush to {self.table}: {e}")
            print(f"[ERROR] BatchClickHouseWriter: Failed to flush to {self.table}: {e}")
            self._buffer = buffer_to_flush + self._buffer
            return False
    
    def _flush_loop(self):
        """后台定时刷新"""
        while self._running:
            time.sleep(0.1)
            if time.time() - self._last_flush >= self.flush_interval:
                with self._lock:
                    if self._buffer:
                        self._do_flush()


class MultiTableBatchWriter:
    """
    多表批量写入管理器
    
    管理多个表的批量写入器
    """
    
    def __init__(self, client, default_batch_size: int = 2000, default_flush_interval: float = 0.5):
        self.client = client
        self.default_batch_size = default_batch_size
        self.default_flush_interval = default_flush_interval
        self._writers: Dict[str, BatchClickHouseWriter] = {}
        self._lock = threading.Lock()
    
    def get_writer(
        self,
        table: str,
        batch_size: Optional[int] = None,
        flush_interval: Optional[float] = None,
        columns: Optional[str] = None
    ) -> BatchClickHouseWriter:
        """
        获取或创建表的写入器

        Args:
            table: 表名
            batch_size: 批量大小
            flush_interval: 刷新间隔
            columns: 列名

        Returns:
            BatchClickHouseWriter: 写入器实例
        """
        with self._lock:
            if table not in self._writers:
                writer = BatchClickHouseWriter(
                    client=self.client,
                    table=table,
                    batch_size=batch_size or self.default_batch_size,
                    flush_interval=flush_interval or self.default_flush_interval,
                    columns=columns
                )
                self._writers[table] = writer
            return self._writers[table]
    
    def add(self, table: str, row: List, columns: Optional[str] = None) -> bool:
        """
        添加数据到指定表

        Args:
            table: 表名
            row: 数据行
            columns: 列名

        Returns:
            bool: 是否触发了刷新
        """
        writer = self.get_writer(table, columns=columns)
        return writer.add(row)
    
    def add_batch(self, table: str, rows: List[List], columns: Optional[str] = None) -> bool:
        """批量添加数据到指定表"""
        writer = self.get_writer(table, columns=columns)
        return writer.add_batch(rows)
    
    def start_all(self):
        """启动所有写入器"""
        with self._lock:
            for writer in self._writers.values():
                writer.start()
    
    def stop_all(self):
        """停止所有写入器"""
        with self._lock:
            for writer in self._writers.values():
                writer.stop()
    
    def get_all_stats(self) -> Dict[str, Dict[str, Any]]:
        """获取所有表的统计信息"""
        with self._lock:
            return {table: writer.get_stats() for table, writer in self._writers.items()}
