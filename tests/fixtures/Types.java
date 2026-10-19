package com.acme.shop.common;

public record Money(long amount, @NotNull String currency) implements Comparable<Money> {
    public Money {
        if (amount < 0) throw new IllegalArgumentException();
    }

    public int compareTo(Money other) {
        return Long.compare(amount, other.amount);
    }
}

enum Status implements Labeled {
    OPEN, CLOSED("c");

    private final String label;

    Status() { this.label = name(); }

    Status(String label) { this.label = label; }

    public String label() { return label; }
}

interface Repo<E, ID> extends Base<E>, Other {
    List<E> findAllByName(String name);

    default boolean exists(ID id) { return false; }
}
