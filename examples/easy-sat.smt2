; easy sat: find 256 bit x and y s.t. x * y overflows
(set-logic QF_BV)
(declare-fun y () (_ BitVec 256))
(declare-fun x () (_ BitVec 256))
(define-fun z () (_ BitVec 256) (bvmul y x))
(assert (bvult z x))
(assert (bvult z y))
(check-sat)
(exit)
