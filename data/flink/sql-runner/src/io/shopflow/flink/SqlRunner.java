package io.shopflow.flink;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.apache.flink.table.api.EnvironmentSettings;
import org.apache.flink.table.api.StatementSet;
import org.apache.flink.table.api.TableEnvironment;

/**
 * Runs a Flink SQL script as one streaming job (application mode, Flink Kubernetes Operator).
 *
 * <p>Statements end with ';' at the end of a line; lines starting with '--' are comments. Every INSERT goes into one
 * statement set, so the script is a single job. {@code ${NAME}} placeholders are replaced by environment variables
 * (credentials, the CDC epoch), with single quotes doubled for SQL string literals; a missing variable is an error.
 * With {@code --explain}, the INSERTs are planned and the plan printed instead of submitted: a CI check that needs
 * neither Kafka nor Postgres.
 */
public final class SqlRunner {
    private static final Pattern PLACEHOLDER = Pattern.compile("\\$\\{([A-Z0-9_]+)}");

    private SqlRunner() {}

    public static void main(String[] args) throws Exception {
        if (args.length < 1 || args.length > 2 || (args.length == 2 && !"--explain".equals(args[1]))) {
            throw new IllegalArgumentException("usage: SqlRunner <script.sql> [--explain]");
        }
        boolean explain = args.length == 2;

        TableEnvironment tableEnv = TableEnvironment.create(EnvironmentSettings.inStreamingMode());
        StatementSet inserts = tableEnv.createStatementSet();
        int insertCount = 0;
        // Comments are dropped before substitution: they may mention placeholders that are not set.
        for (String raw : statements(Files.readString(Path.of(args[0])))) {
            String statement = substitute(raw, System.getenv());
            if (statement.regionMatches(true, 0, "INSERT", 0, 6)) {
                inserts.addInsertSql(statement);
                insertCount++;
            } else {
                tableEnv.executeSql(statement);
            }
        }
        if (insertCount == 0) {
            throw new IllegalArgumentException(args[0] + " has no INSERT statement");
        }
        if (explain) {
            System.out.println(inserts.explain());
        } else {
            inserts.execute();
        }
    }

    static String substitute(String script, Map<String, String> env) {
        Matcher matcher = PLACEHOLDER.matcher(script);
        StringBuilder out = new StringBuilder();
        while (matcher.find()) {
            String value = env.get(matcher.group(1));
            if (value == null) {
                throw new IllegalArgumentException("environment variable " + matcher.group(1) + " is not set");
            }
            matcher.appendReplacement(out, Matcher.quoteReplacement(value.replace("'", "''")));
        }
        matcher.appendTail(out);
        return out.toString();
    }

    static List<String> statements(String script) {
        StringBuilder current = new StringBuilder();
        List<String> statements = new ArrayList<>();
        for (String line : script.split("\n", -1)) {
            String trimmed = line.strip();
            if (trimmed.isEmpty() || trimmed.startsWith("--")) {
                continue;
            }
            if (trimmed.endsWith(";")) {
                current.append(trimmed, 0, trimmed.length() - 1);
                statements.add(current.toString().strip());
                current.setLength(0);
            } else {
                current.append(line).append('\n');
            }
        }
        if (!current.toString().isBlank()) {
            throw new IllegalArgumentException("last statement does not end with ';'");
        }
        return statements;
    }
}
