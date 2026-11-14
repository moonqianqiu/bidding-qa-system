import java.net.InetSocketAddress;
import java.nio.channels.SelectionKey;
import java.nio.channels.Selector;
import java.nio.channels.ServerSocketChannel;

/**
 * Java NIO Selector 健康探针（回环自测）。
 *
 * 背景：2026-11-01 M0 验收发现本机 JDK 的 WEPoll 事件投递全局失聪——内核层 TCP 连接
 * 成功（SYN-ACK 正常），但 selector.select() 永远不报 OP_ACCEPT，导致一切依赖
 * Java NIO 的网络服务（Neo4j 的 Jetty/Netty 等）「监听但不应答」。阻塞式 accept
 * 不受影响（java.net.ServerSocket 正常），PG/Qdrant/Node 等非 Java 服务不受影响。
 *
 * 用法：java NioSelectorTest.java [端口]
 * 判定：客户端（任一 TCP 客户端，如浏览器/curl/telnet/python）连接该端口后，
 *   输出 SELECTOR-OK  → NIO 事件投递正常
 *   输出 SELECTOR-DEAF → 事件投递仍失聪（系统级，需排查代理/驱动/重启）
 */
public class NioSelectorTest {
    public static void main(String[] args) throws Exception {
        int port = args.length > 0 ? Integer.parseInt(args[0]) : 17888;
        ServerSocketChannel ssc = ServerSocketChannel.open();
        ssc.bind(new InetSocketAddress("127.0.0.1", port));
        ssc.configureBlocking(false);
        Selector sel = Selector.open();
        ssc.register(sel, SelectionKey.OP_ACCEPT);
        System.out.println("READY port=" + port + " （请在 10 秒内连接 127.0.0.1:" + port + "）");
        System.out.println(sel.select(10_000) > 0 ? "SELECTOR-OK" : "SELECTOR-DEAF");
        ssc.close();
    }
}
