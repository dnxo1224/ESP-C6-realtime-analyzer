package com.cane.csiadmin;

import java.util.List;
import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;

public interface SessionRepository extends JpaRepository<SessionEntity, Integer> {
    Optional<SessionEntity> findFirstByEndTsIsNullOrderBySessionIdDesc();
    List<SessionEntity> findAllByOrderBySessionIdDesc();
}
