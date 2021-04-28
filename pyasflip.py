# Copyright (c) 2021, Tencent Inc. All rights reserved.

import taichi as ti
import numpy as np
from enum import Enum, auto

class AdvectionType(Enum):
  PIC = 0
  FLIP = 1
  NFLIP = 2
  SFLIP = 3
  APIC = 4
  AFLIP = 5
  ASFLIP = 6
  COUNT = 7

# Advection Parameters
flip_velocity_adjustment = 0.0
flip_position_adjustment_min = 0.0
flip_position_adjustment_max = 0.0
apic_affine_stretching = 1.0
apic_affine_rotation = 1.0
particle_collision = 0.0

def SetupAdvection(advection_type):
  global flip_velocity_adjustment
  global flip_position_adjustment_min, flip_position_adjustment_max
  global apic_affine_stretching, apic_affine_rotation
  global particle_collision
  if advection_type is AdvectionType.PIC:
    flip_velocity_adjustment = 0.0
    flip_position_adjustment_min = 0.0
    flip_position_adjustment_max = 0.0
    apic_affine_stretching = 0.0
    apic_affine_rotation = 0.0
    particle_collision = 0.0
  elif advection_type is AdvectionType.FLIP:
    flip_velocity_adjustment = 0.99
    flip_position_adjustment_min = 0.0
    flip_position_adjustment_max = 0.0
    apic_affine_stretching = 0.0
    apic_affine_rotation = 0.0
    particle_collision = 0.0
  elif advection_type is AdvectionType.NFLIP:
    flip_velocity_adjustment = 0.97
    flip_position_adjustment_min = 1.0
    flip_position_adjustment_max = 1.0
    apic_affine_stretching = 0.0
    apic_affine_rotation = 0.0
    particle_collision = 0.0
  elif advection_type is AdvectionType.SFLIP:
    flip_velocity_adjustment = 0.99
    flip_position_adjustment_min = 0.0
    flip_position_adjustment_max = 1.0
    apic_affine_stretching = 0.0
    apic_affine_rotation = 0.0
    particle_collision = 1.0
  elif advection_type is AdvectionType.APIC:
    flip_velocity_adjustment = 0.0
    flip_position_adjustment_min = 0.0
    flip_position_adjustment_max = 0.0
    apic_affine_stretching = 1.0
    apic_affine_rotation = 1.0
    particle_collision = 0.0
  elif advection_type is AdvectionType.AFLIP:
    flip_velocity_adjustment = 0.99
    flip_position_adjustment_min = 0.0
    flip_position_adjustment_max = 0.0
    apic_affine_stretching = 1.0
    apic_affine_rotation = 1.0
    particle_collision = 0.0
  elif advection_type is AdvectionType.ASFLIP:
    flip_velocity_adjustment = 0.99
    flip_position_adjustment_min = 0.0
    flip_position_adjustment_max = 1.0
    apic_affine_stretching = 1.0
    apic_affine_rotation = 1.0
    particle_collision = 1.0
  return advection_type

# Set Current Integrator
current_advection = SetupAdvection(AdvectionType.ASFLIP)

 # Run Taichi on GPU
ti.init(arch=ti.gpu)
window_res = 512
paused = False

quality = 1 # Use a larger value for higher-res simulations
n_grid = 96 * quality
dx, inv_dx = 1 / n_grid, float(n_grid)

# Particle Source
init_particle_center_x = 0.5
init_particle_center_y = 0.15 + dx * 3.0
init_particle_size_x = 1.0 - dx * 6.0
init_particle_size_y = 0.3

n_particles = int(init_particle_size_x * init_particle_size_y
  * n_grid * n_grid * 9)
frame_dt = 4e-3
dt = 1e-4 / quality
p_vol, p_rho = (dx * 0.5)**2, 1400
p_mass = p_vol * p_rho

# Mechanics parameters
E, nu = 5e5, 0.3 # Young's modulus and Poisson's ratio
kappa_0, mu_0 = E / (3 * (1 - nu * 2)), E / (2 * (1 + nu))
friction_angle = 40.0
sin_phi = ti.sin(friction_angle / 180.0 * 3.141592653)
material_friction = 1.633 * sin_phi / (3.0 - sin_phi)
volume_recovery_rate = 0.5

# Collision Object
init_capsule_center_x = 0.5
init_capsule_center_y = 0.6
init_capsule_vel_y = -1.0
capsule_move_frame = int((0.3 - init_capsule_center_y)
  / init_capsule_vel_y / frame_dt)
capsule_radius = 0.15
capsule_half_length = 0.05
capsule_rotation = ti.Vector.field(1, dtype=float, shape=())
capsule_angular_vel = 80.0
capsule_translation = ti.Vector.field(2, dtype=float, shape=())
capsule_trans_vel = ti.Vector.field(2, dtype=float, shape=())
capsule_friction = 1.0 - ti.exp(-0.4332 * dt / (dx * dx))

ground_friction = 1.0 - ti.exp(-0.1394 * dt / (dx * dx))
side_friction = 0.0

x = ti.Vector.field(2, dtype=float, shape=n_particles) # position
v = ti.Vector.field(2, dtype=float, shape=n_particles) # velocity
C = ti.Matrix.field(2, 2, dtype=float, shape=n_particles) # affine velocity field
F = ti.Matrix.field(2, 2, dtype=float, shape=n_particles) # deformation gradient
Jp = ti.field(dtype=float, shape=n_particles) # plastic deformation
grid_v = ti.Vector.field(2, dtype=float, shape=(n_grid, n_grid)) # grid node momentum/velocity
grid_v0 = ti.Vector.field(2, dtype=float, shape=(n_grid, n_grid)) # grid node previous velocity
grid_m = ti.field(dtype=float, shape=(n_grid, n_grid)) # grid node mass
gravity = ti.Vector.field(2, dtype=float, shape=())
adv_params = ti.Vector.field(6, dtype = float, shape=())

@ti.func
def WorldSpaceToMaterialSpace(x, translation, rotation):
  tmp = x - translation
  X = rotation.transpose() @ tmp
  return X

@ti.func
def SdfCapsule(X, radius, half_length):
  alpha = ti.min(ti.max((X[0] / half_length + 1.0) * 0.5, 0.0), 1.0)
  tmp = ti.Vector([X[0], X[1]])
  tmp[0] += (1.0 - 2.0 * alpha) * half_length
  return tmp.norm() - radius

@ti.func
def SdfNormalCapsule(X, radius, half_length):
  unclamped_alpha = (X[0] / half_length + 1.0) * 0.5
  alpha = ti.min(ti.max(unclamped_alpha, 0.0), 1.0)
  normal = ti.Vector([X[0], X[1]])
  normal[0] += (1.0 - 2.0 * alpha) * half_length
  ltmp = ti.max(1e-12, normal.norm())
  normal[0] /= ltmp;
  normal[1] /= ltmp;
  if unclamped_alpha >= 0.0 and unclamped_alpha <= 1.0:
    normal[0] = 0.0
  return normal

@ti.func
def ProjectDruckerPrager(S: ti.template(), Jp: ti.template()):
  JSe = S[0, 0] * S[1, 1]
  for d in ti.static(range(2)):
    S[d, d] = ti.max(1e-6, ti.abs(S[d, d] * Jp))

  trace_S = S[0, 0] + S[1, 1]
  if trace_S >= 2.0:
    S[0, 0] = 1.0
    S[1, 1] = 1.0
    Jp *= ti.pow(max(1e-6, JSe), volume_recovery_rate)
  else:
    Jp = 1.0
    Je = max(1e-6, S[0, 0] * S[1, 1])
    sqrS_0 = S[0, 0] * S[0, 0]
    sqrS_1 = S[1, 1] * S[1, 1]
    trace_b_2 = (sqrS_0 + sqrS_1) / 2.0
    Je2 = Je * Je
    yield_threshold = -material_friction * kappa_0 * 0.5 * (Je2 - 1.0)
    dev_b0 = sqrS_0 - trace_b_2
    dev_b1 = sqrS_1 - trace_b_2
    norm2_dev_b = dev_b0 * dev_b0 + dev_b1 * dev_b1
    mu_norm_dev_b_bar = mu_0 * ti.sqrt(norm2_dev_b / Je)

    if mu_norm_dev_b_bar > yield_threshold:
      det_b = sqrS_0 * sqrS_1
      det_dev_b = dev_b0 * dev_b1
      lambda_2 = yield_threshold / max(1e-6, mu_norm_dev_b_bar)
      lambda_1 = ti.sqrt(max(0.0, det_b - lambda_2 * lambda_2 * det_dev_b))
      S[0, 0] = ti.sqrt(abs(lambda_1 + lambda_2 * dev_b0))
      S[1, 1] = ti.sqrt(abs(lambda_1 + lambda_2 * dev_b1))

@ti.func
def NeoHookeanElasticity(U, sig):
  J = sig[0, 0] * sig[1, 1]
  mu_J_1_2 = mu_0 * ti.sqrt(J)
  J_prime = kappa_0 * 0.5 * (J * J - 1.0)
  sqrS_1_2 = (sig[0, 0] * sig[0, 0] + sig[1, 1] * sig[1, 1]) / 2.0
  stress = ti.Matrix.identity(float, 2)
  stress[0, 0] = (sig[0, 0] * sig[0, 0] - sqrS_1_2) * mu_J_1_2
  stress[1, 1] = (sig[1, 1] * sig[1, 1] - sqrS_1_2) * mu_J_1_2
  stress = U @ stress @ U.transpose()
  stress[0, 0] += J_prime
  stress[1, 1] += J_prime
  return stress

@ti.kernel
def Substep():
  capsule_rotation[None][0] += capsule_angular_vel * dt
  capsule_translation[None] += capsule_trans_vel[None] * dt

  for i, j in grid_m:
    grid_v[i, j] = [0, 0]
    grid_v0[i, j] = [0, 0]
    grid_m[i, j] = 0

  # Particle state update and scatter to grid (P2G)
  param_apic_str = adv_params[None][3]
  param_apic_rot = adv_params[None][4]
  rc0 = (param_apic_str + param_apic_rot) * 0.5
  rc1 = (param_apic_str - param_apic_rot) * 0.5
  for p in x:
    base = (x[p] * inv_dx - 0.5).cast(int)
    fx = x[p] * inv_dx - base.cast(float)
    # Quadratic kernels  [http://mpm.graphics   Eqn. 123, with x=fx, fx-1,fx-2]
    w = [0.5 * (1.5 - fx) ** 2, 0.75 - (fx - 1) ** 2, 0.5 * (fx - 0.5) ** 2]
    # deformation gradient update
    F[p] = (ti.Matrix.identity(float, 2) + dt * C[p]) @ F[p]
    U, sig, V = ti.svd(F[p])
    # Plasticity flow
    ProjectDruckerPrager(sig, Jp[p])
    # Reconstruct elastic deformation gradient after plasticity
    F[p] = U @ sig @ V.transpose()
    stress = NeoHookeanElasticity(U, sig)
    stress = (-dt * p_vol * 4 * inv_dx * inv_dx) * stress
    affine_without_stress = p_mass * (C[p] * rc0 + C[p].transpose() * rc1)
    affine = stress + affine_without_stress
    for i, j in ti.static(ti.ndrange(3, 3)): # Loop over 3x3 grid node neighborhood
      offset = ti.Vector([i, j])
      dpos = (offset.cast(float) - fx) * dx
      weight = w[i][0] * w[j][1]
      grid_v[base + offset] += weight * (p_mass * v[p] + affine @ dpos)
      grid_v0[base + offset] += weight * (p_mass * v[p] + affine_without_stress @ dpos)
      grid_m[base + offset] += weight * p_mass

  # External force and collision
  for i, j in grid_m:
    if grid_m[i, j] > 0: # No need for epsilon here
      grid_v[i, j] = (1 / grid_m[i, j]) * grid_v[i, j] # Momentum to velocity
      grid_v[i, j] += dt * gravity[None] # gravity
      grid_v0[i, j] = (1 / grid_m[i, j]) * grid_v0[i, j]

      # Boundary conditions at border
      if i < 3 and grid_v[i, j][0] < 0:          
        grid_v[i, j][0] = 0
        grid_v[i, j][1] *= 1.0 - side_friction
      if i > n_grid - 3 and grid_v[i, j][0] > 0: 
        grid_v[i, j][0] = 0
        grid_v[i, j][1] *= 1.0 - side_friction
      if j < 3 and grid_v[i, j][1] < 0:          
        grid_v[i, j][0] *= 1.0 - ground_friction
        grid_v[i, j][1] = 0
      if j > n_grid - 3 and grid_v[i, j][1] > 0: 
        grid_v[i, j][0] *= 1.0 - side_friction
        grid_v[i, j][1] = 0

      # Boundary condition at capsule
      npos = ti.Vector([i, j]).cast(float) * dx
      cap_rot = capsule_rotation[None][0]
      capsule_rotmat = ti.Matrix([
        [ti.cos(cap_rot), -ti.sin(cap_rot)],
        [ti.sin(cap_rot), ti.cos(cap_rot)]])
      local_npos = WorldSpaceToMaterialSpace(npos, capsule_translation[None], capsule_rotmat)
      phi = SdfCapsule(local_npos, capsule_radius, capsule_half_length)
      if phi < 0.0:
        n = capsule_rotmat @ SdfNormalCapsule(local_npos, capsule_radius, capsule_half_length)
        solid_vel = ti.Vector([
          capsule_trans_vel[None][0] - capsule_angular_vel * (npos[1] - capsule_translation[None][1]),
          capsule_trans_vel[None][1] + capsule_angular_vel * (npos[0] - capsule_translation[None][0])])
        diff_vel = solid_vel - grid_v[i, j]
        dotnv = n.dot(diff_vel)
        if dotnv > 0.0:
          dotnv_frac = dotnv * (1.0 - capsule_friction)
          grid_v[i, j] += diff_vel * capsule_friction + n * dotnv_frac

  # grid to particle (G2P)
  param_flip_vel_adj = adv_params[None][0]
  param_flip_pos_adj_min = adv_params[None][1]
  param_flip_pos_adj_max = adv_params[None][2]
  param_part_col = adv_params[None][5] > 0.0
  for p in x:
    base = (x[p] * inv_dx - 0.5).cast(int)
    fx = x[p] * inv_dx - base.cast(float)
    w = [0.5 * (1.5 - fx) ** 2, 0.75 - (fx - 1.0) ** 2, 0.5 * (fx - 0.5) ** 2]
    new_v = ti.Vector.zero(float, 2)
    new_C = ti.Matrix.zero(float, 2, 2)
    for i, j in ti.static(ti.ndrange(3, 3)): # loop over 3x3 grid node neighborhood
      dpos = ti.Vector([i, j]).cast(float) - fx
      g_v = grid_v[base + ti.Vector([i, j])]
      weight = w[i][0] * w[j][1]
      new_v += weight * g_v
      new_C += 4 * inv_dx * weight * g_v.outer_product(dpos)
    # Generalized Advection
    if param_flip_vel_adj > 0.0:
      flip_pos_adj = param_flip_pos_adj_max
      if flip_pos_adj > 0.0 and param_part_col:
        cap_rot = capsule_rotation[None][0]
        capsule_rotmat = ti.Matrix([
          [ti.cos(cap_rot), -ti.sin(cap_rot)],
          [ti.sin(cap_rot), ti.cos(cap_rot)]])
        local_ppos = WorldSpaceToMaterialSpace(x[p], capsule_translation[None], capsule_rotmat)
        phi = SdfCapsule(local_ppos, capsule_radius, capsule_half_length)
        if phi < 0.0:
          n = capsule_rotmat @ SdfNormalCapsule(local_ppos, capsule_radius, capsule_half_length)
          solid_vel = ti.Vector([
            capsule_trans_vel[None][0] - capsule_angular_vel * (x[p][1] - capsule_translation[None][1]),
            capsule_trans_vel[None][1] + capsule_angular_vel * (x[p][0] - capsule_translation[None][0])])
          diff_vel = solid_vel - v[p]
          dotnv = n.dot(diff_vel)
          if dotnv > 0.0:
            flip_pos_adj = 0.0
      if param_flip_pos_adj_min < flip_pos_adj:
        logdJ = new_C.trace() * dt
        J = F[p].determinant()
        if (ti.log(max(1e-15, J)) + logdJ < -0.001):
          flip_pos_adj = param_flip_pos_adj_min
      
      old_v = ti.Vector.zero(float, 2)
      for i, j in ti.static(ti.ndrange(3, 3)):
        g_v0 = grid_v0[base + ti.Vector([i, j])]
        weight = w[i][0] * w[j][1]
        old_v += weight * g_v0

      diff_vel = v[p] - old_v
      v[p] = new_v + param_flip_vel_adj * diff_vel
      x[p] += (new_v + flip_pos_adj * param_flip_vel_adj * diff_vel) * dt
    else:
      v[p] = new_v
      x[p] += new_v * dt

    C[p] = new_C

@ti.kernel
def Reset():
  for i in range(n_particles):
    x[i] = [
    (ti.random() - 0.5) * init_particle_size_x + init_particle_center_x,
    (ti.random() - 0.5) * init_particle_size_y + init_particle_center_y]
    v[i] = [0, 0]
    F[i] = ti.Matrix([[1, 0], [0, 1]])
    Jp[i] = 1
    C[i] = ti.Matrix.zero(float, 2, 2)
  gravity[None] = [0, -9.81]
  capsule_translation[None] = [init_capsule_center_x, init_capsule_center_y]
  capsule_trans_vel[None] = [0, init_capsule_vel_y]
  capsule_rotation[None] = [0.0]

print("[Hint] Press R to reset. <Space> to pause. <Left>/<Right> to switch schemes")
gui = ti.GUI("ASFLIP Demo", res=window_res, background_color=0xffffff)
Reset()
adv_params[None] = [
  flip_velocity_adjustment,
  flip_position_adjustment_min,
  flip_position_adjustment_max,
  apic_affine_stretching,
  apic_affine_rotation,
  particle_collision]

def DrawCapsule(gui, radius, half_length, translation, rotation, color):
  phi = rotation.to_numpy()[0]
  ct = translation.to_numpy()
  psi = np.arctan2(radius, half_length)
  d = np.sqrt(radius * radius + half_length * half_length)
  vert = np.array([
    [ct[0] + d * np.cos(phi + psi), ct[1] + d * np.sin(phi + psi)],
    [ct[0] - d * np.cos(phi - psi), ct[1] - d * np.sin(phi - psi)],
    [ct[0] - d * np.cos(phi + psi), ct[1] - d * np.sin(phi + psi)],
    [ct[0] + d * np.cos(phi - psi), ct[1] + d * np.sin(phi - psi)]])
  end_pos = np.array([
    [ct[0] + half_length * np.cos(phi), ct[1] + half_length * np.sin(phi)],
    [ct[0] - half_length * np.cos(phi), ct[1] - half_length * np.sin(phi)]])
  gui.triangles(
    np.array([vert[0], vert[0]]),
    np.array([vert[1], vert[2]]),
    np.array([vert[2], vert[3]]), color = color)
  gui.circles(end_pos, color = color, radius = radius * window_res)

def PrintScheme():
  print("Advection Scheme: " + current_advection.name)
  print("FLIP Vel. Adj.: " + str(flip_velocity_adjustment))
  print("FLIP Pos. Adj. Min.: " + str(flip_position_adjustment_min))
  print("FLIP Pos. Adj. Max.: " + str(flip_position_adjustment_max))
  print("APIC Aff. Str.: " + str(apic_affine_stretching))
  print("APIC Aff. Rot.: " + str(apic_affine_rotation))
  print("Part. Col.: " + str(particle_collision))

PrintScheme()

frame = 0
wid_frame = gui.label('Frame')
wid_frame.value = frame
while True:
  if gui.get_event(ti.GUI.PRESS):
    if gui.event.key == 'r': 
      Reset()
    elif gui.event.key in [ti.GUI.ESCAPE, ti.GUI.EXIT]: 
      break
    elif gui.event.key == ' ':
      paused = not paused
    elif gui.event.key == ti.GUI.LEFT:
      if current_advection.value == 0:
        current_advection = AdvectionType(AdvectionType.COUNT.value - 1)
      else:
        current_advection = AdvectionType(current_advection.value - 1)        
      SetupAdvection(current_advection)
      PrintScheme()
      adv_params[None] = [
        flip_velocity_adjustment,
        flip_position_adjustment_min,
        flip_position_adjustment_max,
        apic_affine_stretching,
        apic_affine_rotation,
        particle_collision]
    elif gui.event.key == ti.GUI.RIGHT:
      current_advection = AdvectionType(
        (current_advection.value + 1) % AdvectionType.COUNT.value)    
      SetupAdvection(current_advection)
      PrintScheme()
      adv_params[None] = [
        flip_velocity_adjustment,
        flip_position_adjustment_min,
        flip_position_adjustment_max,
        apic_affine_stretching,
        apic_affine_rotation,
        particle_collision]

  if not paused:
    for s in range(int(frame_dt // dt)):
      Substep()
    # if frame == 210: paused = True
    frame += 1
    wid_frame.value = frame
    if frame > capsule_move_frame:
      capsule_trans_vel[None] = [0, 0]

  gui.circles(x.to_numpy(), radius=1.5, color=0x068587)
  DrawCapsule(gui, capsule_radius, capsule_half_length,
    capsule_translation, capsule_rotation, 0x035354)
  gui.show() # Change to gui.show(f'{frame:06d}.png') to write images to disk